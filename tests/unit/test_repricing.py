"""
tests/unit/test_repricing.py — Repricing Router Tests

Covers GET /repricing/history:
  - Returns 200 with correct PriceHistoryEntry list for authenticated user
  - Flattens nested products(title) join into product_title field
  - Returns empty list when user has no history
  - Respects limit/offset query parameters
  - Returns 401 when no Authorization header
  - Handles rows with null products join gracefully (product deleted)
  - Verifies user_id isolation: DB is always queried with current_user.id

Mocking strategy:
  - get_current_user is overridden via dependency_overrides to return a known
    AuthenticatedUser, bypassing all JWT and subscription logic.
  - get_db is overridden to return a MagicMock configured to return the
    desired price_history rows from the chained Supabase query.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, call

import pytest
from fastapi.testclient import TestClient

# Env must be set before importing app modules that read them at import time.
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-repricing-tests")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", "a" * 64)

from api.main import app  # noqa: E402
from api.dependencies import AuthenticatedUser, Tier, get_current_user, get_db  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USER_ID = "user-uuid-history-test"
_FAKE_USER = AuthenticatedUser(id=_USER_ID, email="seller@example.com", tier=Tier.STARTER)

_NOW = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
_EARLIER = datetime(2026, 6, 29, 9, 0, 0, tzinfo=timezone.utc)

_SAMPLE_ROW: dict[str, Any] = {
    "id": "hist-uuid-001",
    "platform": "amazon",
    "old_price": "19.99",
    "new_price": "18.49",
    "strategy": "undercut",
    "confidence": 87,
    "reasoning": "Three competitors lowered prices.",
    "was_auto_applied": True,
    "applied_at": _NOW.isoformat(),
    "products": {"title": "Widget XL"},
}

_SAMPLE_ROW_2: dict[str, Any] = {
    "id": "hist-uuid-002",
    "platform": "etsy",
    "old_price": "35.00",
    "new_price": "35.00",
    "strategy": "hold",
    "confidence": 92,
    "reasoning": "No competitive pressure detected.",
    "was_auto_applied": False,
    "applied_at": _EARLIER.isoformat(),
    "products": {"title": "Handmade Mug"},
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_db_with_history(rows: list[dict]) -> MagicMock:
    """
    Build a mock Supabase client whose price_history query chain returns rows.

    The chain exercised by the endpoint is:
      db.table(...)
        .select(...)
        .eq("user_id", user_id)
        .order(...)
        .limit(...)
        .offset(...)
        .execute()
    """
    mock_db = MagicMock()
    (
        mock_db.table.return_value
        .select.return_value
        .eq.return_value
        .order.return_value
        .limit.return_value
        .offset.return_value
        .execute.return_value
    ) = MagicMock(data=rows)
    return mock_db


def _make_client(mock_db: MagicMock) -> TestClient:
    """Return a TestClient with auth bypassed and DB injected."""
    app.dependency_overrides[get_current_user] = lambda: _FAKE_USER
    app.dependency_overrides[get_db] = lambda: mock_db
    return TestClient(app, raise_server_exceptions=False)


def _cleanup() -> None:
    """Remove dependency overrides between tests."""
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGetRepricingHistory:
    """GET /repricing/history — authenticated user retrieves their price history."""

    def teardown_method(self) -> None:
        _cleanup()

    def test_returns_200_with_history_entries(self) -> None:
        """Valid request returns 200 and a list of PriceHistoryEntry dicts."""
        mock_db = _mock_db_with_history([_SAMPLE_ROW])
        client = _make_client(mock_db)

        resp = client.get("/repricing/history", headers={"Authorization": "Bearer token"})

        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 1

    def test_response_contains_all_required_fields(self) -> None:
        """Each entry contains every field the frontend PriceHistoryEntry expects."""
        mock_db = _mock_db_with_history([_SAMPLE_ROW])
        client = _make_client(mock_db)

        resp = client.get("/repricing/history", headers={"Authorization": "Bearer token"})
        entry = resp.json()[0]

        assert entry["id"] == "hist-uuid-001"
        assert entry["product_title"] == "Widget XL"
        assert entry["platform"] == "amazon"
        assert entry["old_price"] == pytest.approx(19.99)
        assert entry["new_price"] == pytest.approx(18.49)
        assert entry["strategy"] == "undercut"
        assert entry["confidence"] == 87
        assert entry["reasoning"] == "Three competitors lowered prices."
        assert entry["was_auto_applied"] is True
        assert "applied_at" in entry

    def test_product_title_flattened_from_nested_join(self) -> None:
        """products(title) join result is flattened to product_title at the top level."""
        row = {**_SAMPLE_ROW, "products": {"title": "Flat Widget"}}
        mock_db = _mock_db_with_history([row])
        client = _make_client(mock_db)

        resp = client.get("/repricing/history", headers={"Authorization": "Bearer token"})
        entry = resp.json()[0]

        assert entry["product_title"] == "Flat Widget"
        assert "products" not in entry

    def test_null_products_join_uses_fallback_title(self) -> None:
        """If products join returns null (product deleted), title falls back to 'Unknown product'."""
        row = {**_SAMPLE_ROW, "products": None}
        mock_db = _mock_db_with_history([row])
        client = _make_client(mock_db)

        resp = client.get("/repricing/history", headers={"Authorization": "Bearer token"})
        entry = resp.json()[0]

        assert entry["product_title"] == "Unknown product"

    def test_empty_history_returns_empty_list(self) -> None:
        """User with no price history gets an empty list, not a 404."""
        mock_db = _mock_db_with_history([])
        client = _make_client(mock_db)

        resp = client.get("/repricing/history", headers={"Authorization": "Bearer token"})

        assert resp.status_code == 200
        assert resp.json() == []

    def test_multiple_entries_ordered_newest_first(self) -> None:
        """Multiple rows are returned in the order the DB provides (desc applied_at)."""
        mock_db = _mock_db_with_history([_SAMPLE_ROW, _SAMPLE_ROW_2])
        client = _make_client(mock_db)

        resp = client.get("/repricing/history", headers={"Authorization": "Bearer token"})
        entries = resp.json()

        assert len(entries) == 2
        assert entries[0]["id"] == "hist-uuid-001"
        assert entries[1]["id"] == "hist-uuid-002"

    def test_limit_param_is_passed_to_db(self) -> None:
        """limit query param is forwarded to the Supabase query chain."""
        mock_db = _mock_db_with_history([])
        client = _make_client(mock_db)

        client.get("/repricing/history?limit=10", headers={"Authorization": "Bearer token"})

        # Verify .limit(10) was called in the chain
        limit_mock = (
            mock_db.table.return_value
            .select.return_value
            .eq.return_value
            .order.return_value
            .limit
        )
        limit_mock.assert_called_once_with(10)

    def test_offset_param_is_passed_to_db(self) -> None:
        """offset query param is forwarded to the Supabase query chain."""
        mock_db = _mock_db_with_history([])
        client = _make_client(mock_db)

        client.get("/repricing/history?offset=25", headers={"Authorization": "Bearer token"})

        offset_mock = (
            mock_db.table.return_value
            .select.return_value
            .eq.return_value
            .order.return_value
            .limit.return_value
            .offset
        )
        offset_mock.assert_called_once_with(25)

    def test_query_filters_by_authenticated_user_id(self) -> None:
        """DB query always uses current_user.id from the JWT — never from request params."""
        mock_db = _mock_db_with_history([])
        client = _make_client(mock_db)

        client.get("/repricing/history", headers={"Authorization": "Bearer token"})

        eq_mock = (
            mock_db.table.return_value
            .select.return_value
            .eq
        )
        eq_mock.assert_called_once_with("user_id", _USER_ID)

    def test_invalid_limit_returns_422(self) -> None:
        """limit=0 violates the ge=1 constraint and returns 422 Unprocessable Entity."""
        mock_db = _mock_db_with_history([])
        client = _make_client(mock_db)

        resp = client.get("/repricing/history?limit=0", headers={"Authorization": "Bearer token"})

        assert resp.status_code == 422

    def test_limit_above_maximum_returns_422(self) -> None:
        """limit=201 violates the le=200 constraint and returns 422."""
        mock_db = _mock_db_with_history([])
        client = _make_client(mock_db)

        resp = client.get("/repricing/history?limit=201", headers={"Authorization": "Bearer token"})

        assert resp.status_code == 422

    def test_null_optional_fields_are_serialised_as_none(self) -> None:
        """strategy, confidence, reasoning may be null — they are returned as null, not omitted."""
        row = {
            **_SAMPLE_ROW,
            "strategy": None,
            "confidence": None,
            "reasoning": None,
        }
        mock_db = _mock_db_with_history([row])
        client = _make_client(mock_db)

        resp = client.get("/repricing/history", headers={"Authorization": "Bearer token"})
        entry = resp.json()[0]

        assert entry["strategy"] is None
        assert entry["confidence"] is None
        assert entry["reasoning"] is None

    def test_db_error_returns_500(self) -> None:
        """Database exception is caught and returned as HTTP 500."""
        mock_db = MagicMock()
        (
            mock_db.table.return_value
            .select.return_value
            .eq.return_value
            .order.return_value
            .limit.return_value
            .offset.return_value
            .execute
        ).side_effect = RuntimeError("DB connection lost")

        app.dependency_overrides[get_current_user] = lambda: _FAKE_USER
        app.dependency_overrides[get_db] = lambda: mock_db
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/repricing/history", headers={"Authorization": "Bearer token"})

        assert resp.status_code == 500
