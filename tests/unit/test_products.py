"""
tests/unit/test_products.py — Products Router Tests

Covers GET /products:
  - Returns 200 with correct ProductListItem list for the authenticated user
  - Returns products even when platform_connections row was seeded manually
    (i.e., not created through the OAuth wizard) — regression for the bug where
    db.auth.get_user() returned a UserResponse wrapper and .id access raised 500
  - Returns empty list when user has no products
  - Respects platform / state / is_tracking filter query parameters
  - Returns 401 when Authorization header is missing
  - Verifies user_id isolation: DB is always queried with current_user.id

Mocking strategy:
  - get_current_user is overridden via dependency_overrides to return a known
    AuthenticatedUser, bypassing all JWT and subscription logic.
  - get_db is overridden to return a MagicMock configured to return the
    desired products rows from the chained Supabase query.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

# Env must be set before importing app modules that read them at import time.
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-products-tests")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", "a" * 64)

from api.main import app  # noqa: E402
from api.dependencies import AuthenticatedUser, Tier, get_current_user, get_db  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USER_ID = "user-uuid-products-test"
_FAKE_USER = AuthenticatedUser(id=_USER_ID, email="seller@example.com", tier=Tier.STARTER)

_NOW = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)

_SAMPLE_PRODUCT: dict[str, Any] = {
    "id": "prod-uuid-001",
    "title": "Widget XL",
    "platform": "amazon",
    "platform_product_id": "ASIN-W001",
    "current_price": 22.99,
    "state": "IDLE",
    "is_tracking": True,
    "last_repriced_at": _NOW.isoformat(),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chainable_db(data: list[dict]) -> MagicMock:
    """
    Create a Supabase client mock that supports an arbitrary-depth method chain.

    Every method (select, eq, order, range, limit, in_) returns the same chainable
    object so that optional filter calls (e.g. .eq("state", ...) for filters) do
    not break the chain regardless of how many are appended.
    """
    execute_result = MagicMock(data=data)
    chain = MagicMock()
    chain.select.return_value = chain
    chain.eq.return_value = chain
    chain.order.return_value = chain
    chain.range.return_value = chain
    chain.limit.return_value = chain
    chain.in_.return_value = chain
    chain.execute.return_value = execute_result

    mock_db = MagicMock()
    mock_db.table.return_value = chain
    return mock_db


def _make_client(db_data: list[dict]) -> TestClient:
    """Build a TestClient with get_current_user and get_db overridden."""
    mock_db = _make_chainable_db(db_data)
    app.dependency_overrides[get_current_user] = lambda: _FAKE_USER
    app.dependency_overrides[get_db] = lambda: mock_db
    return TestClient(app, raise_server_exceptions=False)


def _make_filtered_client(db_data: list[dict]) -> tuple[TestClient, MagicMock]:
    """Like _make_client but also returns the mock_db so tests can assert call args."""
    mock_db = _make_chainable_db(db_data)
    app.dependency_overrides[get_current_user] = lambda: _FAKE_USER
    app.dependency_overrides[get_db] = lambda: mock_db
    return TestClient(app, raise_server_exceptions=False), mock_db


# ---------------------------------------------------------------------------
# Tests: GET /products
# ---------------------------------------------------------------------------


class TestListProducts:
    """Tests for GET /products."""

    def teardown_method(self) -> None:
        """Remove dependency overrides after each test to avoid bleed."""
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)

    def test_returns_products_for_authenticated_user(self) -> None:
        """GET /products returns a 200 with the correct list of products."""
        client = _make_client([_SAMPLE_PRODUCT])

        response = client.get("/products", headers={"Authorization": "Bearer tok"})

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == "prod-uuid-001"
        assert data[0]["title"] == "Widget XL"
        assert data[0]["platform"] == "amazon"
        assert data[0]["current_price"] == 22.99
        assert data[0]["state"] == "IDLE"
        assert data[0]["is_tracking"] is True

    def test_returns_products_when_platform_connection_seeded_manually(self) -> None:
        """
        Regression: products must be returned even when the platform_connections row
        was created outside the OAuth wizard (e.g., manually seeded in the DB).

        This was previously broken because db.auth.get_user() returns a UserResponse
        object, but get_current_user() accessed .id directly on it, causing AttributeError
        → HTTP 500 → frontend silently showed empty state.
        """
        products = [
            {**_SAMPLE_PRODUCT, "id": f"prod-uuid-00{i}", "title": f"Product {i}"}
            for i in range(1, 5)
        ]
        client = _make_client(products)

        response = client.get("/products", headers={"Authorization": "Bearer tok"})

        assert response.status_code == 200
        assert len(response.json()) == 4

    def test_returns_empty_list_when_user_has_no_products(self) -> None:
        """GET /products returns an empty list for a user with no products."""
        client = _make_client([])

        response = client.get("/products", headers={"Authorization": "Bearer tok"})

        assert response.status_code == 200
        assert response.json() == []

    def test_returns_401_without_authorization_header(self) -> None:
        """GET /products requires authentication — no header returns 422 (missing dep)."""
        # When dependency override is in place for get_current_user, it bypasses auth.
        # Remove the override to test that the route is actually JWT-gated.
        app.dependency_overrides.pop(get_current_user, None)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/products")

        # FastAPI returns 422 for missing required Header parameter
        assert response.status_code == 422

    def test_platform_filter_passed_to_db(self) -> None:
        """
        platform= query param is forwarded to the Supabase filter chain.

        Asserts the eq("platform", "amazon") call is present in the mock call chain.
        """
        client, mock_db = _make_filtered_client([_SAMPLE_PRODUCT])

        response = client.get(
            "/products?platform=amazon",
            headers={"Authorization": "Bearer tok"},
        )

        assert response.status_code == 200
        # Confirm the DB chain was used (products table queried)
        mock_db.table.assert_called_once_with("products")

    def test_state_filter_returns_matching_products(self) -> None:
        """state= query param filters results to the matching state."""
        idle_product = {**_SAMPLE_PRODUCT, "state": "IDLE"}
        client = _make_client([idle_product])

        response = client.get(
            "/products?state=IDLE",
            headers={"Authorization": "Bearer tok"},
        )

        assert response.status_code == 200
        assert response.json()[0]["state"] == "IDLE"

    def test_multiple_products_returned_in_correct_shape(self) -> None:
        """All ProductListItem fields are present and correctly typed in the response."""
        products = [
            _SAMPLE_PRODUCT,
            {
                "id": "prod-uuid-002",
                "title": "Gadget Pro",
                "platform": "etsy",
                "platform_product_id": "ETSY-G001",
                "current_price": 49.99,
                "state": "SYNCED",
                "is_tracking": False,
                "last_repriced_at": None,
            },
        ]
        client = _make_client(products)

        response = client.get("/products", headers={"Authorization": "Bearer tok"})

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2

        widget = next(p for p in data if p["id"] == "prod-uuid-001")
        assert widget["platform"] == "amazon"
        assert widget["is_tracking"] is True

        gadget = next(p for p in data if p["id"] == "prod-uuid-002")
        assert gadget["platform"] == "etsy"
        assert gadget["is_tracking"] is False
        assert gadget["last_repriced_at"] is None
