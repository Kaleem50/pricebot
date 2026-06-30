"""
tests/unit/test_auth_guard.py — Unit Tests for JWT Validation and Tier Enforcement

Tests cover:
  - ``get_current_user``: valid JWT, expired JWT, missing header, invalid format,
    subscription lookup (active/inactive/missing), tier parsing.
  - ``require_tier``: sufficient tier passes, insufficient tier raises 403.
  - ``Tier`` enum: ordering and ``from_db()`` parsing.

Mocking strategy:
  - ``JWT_SECRET`` is set as an environment variable to avoid needing a real secret.
  - ``jwt.decode`` in ``api.dependencies`` is patched to control payload content.
  - ``get_db`` is overridden via FastAPI dependency injection to return a mock
    Supabase client without making real network calls.
"""

from __future__ import annotations

import os
import time
from unittest.mock import MagicMock, patch

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from api.dependencies import AuthenticatedUser, Tier, get_current_user, get_db
from api.middleware.auth_guard import require_tier

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_USER_ID = "user-uuid-1234"
_VALID_EMAIL = "seller@example.com"
_JWT_SECRET = "test-secret-key-that-is-at-least-32-chars-long"

_VALID_PAYLOAD = {
    "sub": _VALID_USER_ID,
    "email": _VALID_EMAIL,
    "aud": "authenticated",
    "exp": int(time.time()) + 3600,
    "iat": int(time.time()),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token(payload: dict | None = None) -> str:
    """Encode a JWT with the given payload using the test secret."""
    return jwt.encode(payload or _VALID_PAYLOAD, _JWT_SECRET, algorithm="HS256")


def _make_user_response(
    user_id: str = _VALID_USER_ID,
    email: str = _VALID_EMAIL,
) -> MagicMock:
    """
    Simulate a Supabase UserResponse wrapping an inner User.

    Uses ``spec`` to prevent MagicMock from auto-creating attributes:
      - outer mock only exposes ``.user`` (matches real UserResponse)
      - inner mock only exposes ``.id`` and ``.email``

    This ensures ``getattr(response, "user", response)`` in
    ``get_current_user()`` correctly resolves to the inner user object.
    """
    mock_user = MagicMock(spec=["id", "email"])
    mock_user.id = user_id
    mock_user.email = email
    mock_response = MagicMock(spec=["user"])
    mock_response.user = mock_user
    return mock_response


def _mock_db_with_subscription(tier: str = "starter", status: str = "active") -> MagicMock:
    """Return a mock Supabase client with auth.get_user() and subscription lookup."""
    mock_db = MagicMock()

    # auth.get_user() returns a UserResponse wrapping a User object
    mock_db.auth.get_user.return_value = _make_user_response()

    # Mock table queries for subscription lookup
    (
        mock_db.table.return_value
        .select.return_value
        .eq.return_value
        .execute.return_value
    ) = MagicMock(data=[{"tier": tier, "status": status}])

    return mock_db


def _mock_db_no_subscription() -> MagicMock:
    """Return a mock Supabase client that returns no subscription rows."""
    mock_db = MagicMock()
    (
        mock_db.table.return_value
        .select.return_value
        .eq.return_value
        .execute.return_value
    ) = MagicMock(data=[])
    return mock_db


def _make_auth_app(mock_db: MagicMock) -> tuple[FastAPI, TestClient]:
    """
    Build a minimal FastAPI app with a /me route protected by ``get_current_user``.

    Returns the app and a ``TestClient`` pre-configured for use with
    ``raise_server_exceptions=False`` so HTTP error codes are surfaced normally.
    """
    app = FastAPI()
    app.dependency_overrides[get_db] = lambda: mock_db

    @app.get("/me")
    async def me(user: AuthenticatedUser = Depends(get_current_user)) -> dict:
        return {"id": user.id, "tier": user.tier.name, "email": user.email}

    return app, TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Tests: get_current_user
# ---------------------------------------------------------------------------


class TestGetCurrentUser:
    """Tests for the ``get_current_user`` FastAPI dependency."""

    def test_valid_token_returns_authenticated_user(self) -> None:
        """Valid JWT with active subscription returns AuthenticatedUser with correct tier."""
        mock_db = _mock_db_with_subscription(tier="growth", status="active")
        app, client = _make_auth_app(mock_db)

        response = client.get("/me", headers={"Authorization": "Bearer sometoken"})

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == _VALID_USER_ID
        assert data["tier"] == "GROWTH"

    def test_missing_authorization_header_returns_422(self) -> None:
        """Request without Authorization header is rejected with 422 (FastAPI validation)."""
        mock_db = _mock_db_with_subscription()
        app, client = _make_auth_app(mock_db)

        response = client.get("/me")
        assert response.status_code == 422

    def test_non_bearer_prefix_returns_401(self) -> None:
        """Authorization header without 'Bearer ' prefix is rejected with 401."""
        mock_db = _mock_db_with_subscription()
        app, client = _make_auth_app(mock_db)

        response = client.get("/me", headers={"Authorization": "Token abc123"})

        assert response.status_code == 401

    def test_expired_token_returns_401(self) -> None:
        """An expired JWT raises HTTP 401."""
        mock_db = _mock_db_with_subscription()
        mock_db.auth.get_user.side_effect = Exception("Token expired")
        app, client = _make_auth_app(mock_db)

        response = client.get("/me", headers={"Authorization": "Bearer expired_token"})

        assert response.status_code == 401

    def test_tampered_token_returns_401(self) -> None:
        """A JWT with invalid signature or tampering raises HTTP 401."""
        mock_db = _mock_db_with_subscription()
        mock_db.auth.get_user.side_effect = Exception("Invalid token")
        app, client = _make_auth_app(mock_db)

        response = client.get(
            "/me",
            headers={"Authorization": "Bearer tampered_token"},
        )

        assert response.status_code == 401

    def test_inactive_subscription_returns_403(self) -> None:
        """User with a canceled subscription is rejected with HTTP 403."""
        mock_db = _mock_db_with_subscription(tier="starter", status="canceled")
        app, client = _make_auth_app(mock_db)

        response = client.get("/me", headers={"Authorization": "Bearer tok"})

        assert response.status_code == 403
        assert "inactive" in response.json()["detail"].lower()

    def test_no_subscription_defaults_to_starter(self) -> None:
        """New user with no subscription row is allowed and defaults to STARTER tier."""
        mock_db = _mock_db_no_subscription()
        mock_db.auth.get_user.return_value = _make_user_response()

        app, client = _make_auth_app(mock_db)

        response = client.get("/me", headers={"Authorization": "Bearer tok"})

        assert response.status_code == 200
        assert response.json()["tier"] == "STARTER"

    def test_user_id_sourced_from_jwt_sub_not_request(self) -> None:
        """
        user_id in the response must match the JWT 'sub' claim via auth.get_user().
        An attacker-supplied user_id in query params is never used for authorization.
        """
        controlled_user_id = "real-user-id-from-token"
        mock_db = _mock_db_with_subscription()
        mock_db.auth.get_user.return_value = _make_user_response(user_id=controlled_user_id)

        app, client = _make_auth_app(mock_db)

        response = client.get(
            "/me",
            headers={"Authorization": "Bearer tok"},
            params={"user_id": "attacker-supplied-id"},
        )

        assert response.status_code == 200
        assert response.json()["id"] == controlled_user_id
        assert response.json()["id"] != "attacker-supplied-id"

    def test_trialing_subscription_is_accepted(self) -> None:
        """Users on a free trial ('trialing') are permitted and return their tier."""
        mock_db = _mock_db_with_subscription(tier="pro", status="trialing")
        app, client = _make_auth_app(mock_db)

        response = client.get("/me", headers={"Authorization": "Bearer tok"})

        assert response.status_code == 200
        assert response.json()["tier"] == "PRO"

    def test_past_due_subscription_returns_403(self) -> None:
        """Users with a past_due subscription are denied access."""
        mock_db = _mock_db_with_subscription(tier="growth", status="past_due")
        app, client = _make_auth_app(mock_db)

        response = client.get("/me", headers={"Authorization": "Bearer tok"})

        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Tests: require_tier
# ---------------------------------------------------------------------------


def _make_tier_gated_app(minimum_tier: Tier, user_tier: Tier) -> TestClient:
    """
    Build a test app with a tier-gated route where ``get_current_user``
    is overridden to return a user with ``user_tier``.
    """
    app = FastAPI()

    mock_user = AuthenticatedUser(
        id=_VALID_USER_ID,
        email=_VALID_EMAIL,
        tier=user_tier,
    )

    async def override_get_current_user() -> AuthenticatedUser:
        return mock_user

    app.dependency_overrides[get_current_user] = override_get_current_user

    @app.get("/gated")
    async def gated(_: None = Depends(require_tier(minimum_tier))) -> dict:
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


class TestRequireTier:
    """Tests for the ``require_tier`` dependency factory."""

    def test_exact_tier_match_is_allowed(self) -> None:
        """User with exactly the required tier is permitted."""
        client = _make_tier_gated_app(Tier.GROWTH, Tier.GROWTH)
        response = client.get("/gated", headers={"Authorization": "Bearer tok"})
        assert response.status_code == 200

    def test_higher_tier_is_allowed(self) -> None:
        """PRO user can access a GROWTH-gated route."""
        client = _make_tier_gated_app(Tier.GROWTH, Tier.PRO)
        response = client.get("/gated", headers={"Authorization": "Bearer tok"})
        assert response.status_code == 200

    def test_lower_tier_returns_403(self) -> None:
        """STARTER user cannot access a GROWTH-gated route."""
        client = _make_tier_gated_app(Tier.GROWTH, Tier.STARTER)
        response = client.get("/gated", headers={"Authorization": "Bearer tok"})
        assert response.status_code == 403
        detail = response.json()["detail"].lower()
        assert "growth" in detail

    def test_starter_cannot_access_pro_route(self) -> None:
        """STARTER user cannot access a PRO-gated route."""
        client = _make_tier_gated_app(Tier.PRO, Tier.STARTER)
        response = client.get("/gated", headers={"Authorization": "Bearer tok"})
        assert response.status_code == 403

    def test_growth_cannot_access_pro_route(self) -> None:
        """GROWTH user cannot access a PRO-gated route."""
        client = _make_tier_gated_app(Tier.PRO, Tier.GROWTH)
        response = client.get("/gated", headers={"Authorization": "Bearer tok"})
        assert response.status_code == 403

    def test_starter_can_access_starter_route(self) -> None:
        """STARTER user can access a STARTER-gated route."""
        client = _make_tier_gated_app(Tier.STARTER, Tier.STARTER)
        response = client.get("/gated", headers={"Authorization": "Bearer tok"})
        assert response.status_code == 200

    def test_403_detail_names_required_plan(self) -> None:
        """HTTP 403 response must mention the required plan tier by name."""
        client = _make_tier_gated_app(Tier.PRO, Tier.STARTER)
        response = client.get("/gated", headers={"Authorization": "Bearer tok"})
        assert response.status_code == 403
        assert "pro" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Tests: Tier enum
# ---------------------------------------------------------------------------


class TestTierEnum:
    """Tests for the ``Tier`` enum ordering and ``from_db()`` parsing."""

    def test_tier_ordering(self) -> None:
        """Tier members are orderable by integer value: STARTER < GROWTH < PRO."""
        assert Tier.STARTER < Tier.GROWTH
        assert Tier.GROWTH < Tier.PRO
        assert Tier.STARTER < Tier.PRO

    def test_from_db_parses_starter(self) -> None:
        assert Tier.from_db("starter") == Tier.STARTER

    def test_from_db_parses_growth(self) -> None:
        assert Tier.from_db("growth") == Tier.GROWTH

    def test_from_db_parses_pro(self) -> None:
        assert Tier.from_db("pro") == Tier.PRO

    def test_from_db_is_case_insensitive(self) -> None:
        """``from_db()`` handles UPPER, Title, and lower case input."""
        assert Tier.from_db("STARTER") == Tier.STARTER
        assert Tier.from_db("Growth") == Tier.GROWTH
        assert Tier.from_db("PRO") == Tier.PRO

    def test_from_db_raises_on_unknown_value(self) -> None:
        """``from_db()`` raises ``ValueError`` for unrecognised tier strings."""
        with pytest.raises(ValueError, match="Unknown tier"):
            Tier.from_db("enterprise")

    def test_tier_integer_values(self) -> None:
        """STARTER=1, GROWTH=2, PRO=3 — order is critical for comparison logic."""
        assert int(Tier.STARTER) == 1
        assert int(Tier.GROWTH) == 2
        assert int(Tier.PRO) == 3
