"""
tests/unit/test_auth.py — Authentication Endpoint Tests

Tests for POST /auth/register with email confirmation both enabled and disabled.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from supabase_auth.errors import AuthApiError

# ---------------------------------------------------------------------------
# Env setup (must precede local imports)
# ---------------------------------------------------------------------------

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-auth-tests")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", "a" * 64)

from api.main import app  # noqa: E402
from api.dependencies import get_db  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_db() -> MagicMock:
    """Return a mock Supabase Client."""
    return MagicMock()


def _make_user_response(user_id: str = "user-uuid-001", email: str = "test@example.com") -> MagicMock:
    """Mock a Supabase User object."""
    user = MagicMock()
    user.id = user_id
    user.email = email
    return user


def _make_session_response(
    access_token: str = "access_token_123",
    refresh_token: str = "refresh_token_456",
) -> MagicMock:
    """Mock a Supabase Session object."""
    session = MagicMock()
    session.access_token = access_token
    session.refresh_token = refresh_token
    return session


# ===========================================================================
# Tests: Registration with immediate signup (no email confirmation)
# ===========================================================================


class TestRegisterWithImmediateSignup:
    """Email confirmation disabled → session present → 201 with tokens."""

    def test_successful_registration_returns_tokens(self):
        """Signup succeeds and session is present → return tokens and signin message."""
        db_mock = _mock_db()

        user = _make_user_response("user-xyz", "alice@example.com")
        session = _make_session_response("token-111", "refresh-222")

        response_obj = MagicMock()
        response_obj.user = user
        response_obj.session = session

        db_mock.auth.sign_up = MagicMock(return_value=response_obj)

        app.dependency_overrides[get_db] = lambda: db_mock
        try:
            client = TestClient(app)
            resp = client.post(
                "/auth/register",
                json={"email": "alice@example.com", "password": "SecurePass123"},
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 201
        data = resp.json()
        assert data["email_confirmation_required"] is False
        assert data["access_token"] == "token-111"
        assert data["refresh_token"] == "refresh-222"
        assert data["user_id"] == "user-xyz"
        assert data["email"] == "alice@example.com"
        assert "signed in" in data["message"].lower()

    def test_weak_password_returns_400(self):
        """Invalid password → Supabase rejects → 400 with error detail."""
        db_mock = _mock_db()

        db_mock.auth.sign_up = MagicMock(
            side_effect=AuthApiError(
                "Password too weak (missing uppercase)",
                status=400,
                code="weak_password",
            )
        )

        app.dependency_overrides[get_db] = lambda: db_mock
        try:
            client = TestClient(app)
            resp = client.post(
                "/auth/register",
                json={"email": "alice@example.com", "password": "weakpassword123"},
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 400
        assert "weak" in resp.json().get("detail", "").lower()

    def test_duplicate_email_returns_400(self):
        """Email already in use → Supabase rejects → 400."""
        db_mock = _mock_db()

        db_mock.auth.sign_up = MagicMock(
            side_effect=AuthApiError(
                "User already registered",
                status=400,
                code="user_already_exists",
            )
        )

        app.dependency_overrides[get_db] = lambda: db_mock
        try:
            client = TestClient(app)
            resp = client.post(
                "/auth/register",
                json={"email": "alice@example.com", "password": "SecurePass123"},
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 400
        assert "already" in resp.json().get("detail", "").lower()


# ===========================================================================
# Tests: Registration with email confirmation required
# ===========================================================================


class TestRegisterWithEmailConfirmation:
    """Email confirmation enabled → no session → 201 with confirmation message."""

    def test_registration_with_confirmation_required(self):
        """Signup succeeds but session is None → email confirmation required."""
        db_mock = _mock_db()

        user = _make_user_response("user-abc", "bob@example.com")
        # No session — email confirmation required
        response_obj = MagicMock()
        response_obj.user = user
        response_obj.session = None

        db_mock.auth.sign_up = MagicMock(return_value=response_obj)

        app.dependency_overrides[get_db] = lambda: db_mock
        try:
            client = TestClient(app)
            resp = client.post(
                "/auth/register",
                json={"email": "bob@example.com", "password": "SecurePass123"},
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 201
        data = resp.json()
        assert data["email_confirmation_required"] is True
        assert data["access_token"] is None
        assert data["refresh_token"] is None
        assert data["user_id"] == "user-abc"
        assert data["email"] == "bob@example.com"
        assert "check your email" in data["message"].lower()

    def test_confirmation_uses_user_email_as_fallback(self):
        """If Supabase user.email is missing, use the request email."""
        db_mock = _mock_db()

        user = _make_user_response("user-def", email="")  # Empty email
        response_obj = MagicMock()
        response_obj.user = user
        response_obj.session = None

        db_mock.auth.sign_up = MagicMock(return_value=response_obj)

        app.dependency_overrides[get_db] = lambda: db_mock
        try:
            client = TestClient(app)
            resp = client.post(
                "/auth/register",
                json={"email": "charlie@example.com", "password": "SecurePass123"},
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 201
        data = resp.json()
        assert data["email"] == "charlie@example.com"
        assert data["email_confirmation_required"] is True


# ===========================================================================
# Tests: Edge cases
# ===========================================================================


class TestRegisterEdgeCases:
    """Handle malformed responses, missing user, etc."""

    def test_registration_with_no_user_returns_400(self):
        """Supabase returns no user → internal error."""
        db_mock = _mock_db()

        response_obj = MagicMock()
        response_obj.user = None
        response_obj.session = None

        db_mock.auth.sign_up = MagicMock(return_value=response_obj)

        app.dependency_overrides[get_db] = lambda: db_mock
        try:
            client = TestClient(app)
            resp = client.post(
                "/auth/register",
                json={"email": "test@example.com", "password": "SecurePass123"},
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 400

    def test_unexpected_exception_returns_500(self):
        """Unexpected error from Supabase → 500."""
        db_mock = _mock_db()
        db_mock.auth.sign_up = MagicMock(side_effect=RuntimeError("DB connection lost"))

        app.dependency_overrides[get_db] = lambda: db_mock
        try:
            client = TestClient(app)
            resp = client.post(
                "/auth/register",
                json={"email": "test@example.com", "password": "SecurePass123"},
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 500
        assert "service error" in resp.json().get("detail", "").lower()

    def test_invalid_email_format_returns_422(self):
        """Pydantic validation fails on malformed email."""
        db_mock = _mock_db()
        app.dependency_overrides[get_db] = lambda: db_mock
        try:
            client = TestClient(app)
            resp = client.post(
                "/auth/register",
                json={"email": "not-an-email", "password": "SecurePass123"},
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 422

    def test_missing_password_returns_422(self):
        """Missing password → Pydantic validation fails."""
        db_mock = _mock_db()
        app.dependency_overrides[get_db] = lambda: db_mock
        try:
            client = TestClient(app)
            resp = client.post(
                "/auth/register",
                json={"email": "test@example.com"},
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 422
