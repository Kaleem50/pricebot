"""
tests/unit/test_beta.py — Unit tests for api/routers/beta.py

Covers:
  - POST /beta/request: successful submission
  - POST /beta/request: duplicate email returns 200 (upsert idempotency)
  - GET /beta/requests: correct OPERATOR_SECRET header grants access
  - GET /beta/requests: wrong/missing OPERATOR_SECRET → 403
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.dependencies import get_db
from api.routers.beta import router

# ---------------------------------------------------------------------------
# App setup for isolated testing
# ---------------------------------------------------------------------------

app = FastAPI()
app.include_router(router)
client = TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_db() -> MagicMock:
    """Return a mock Supabase client that silently succeeds on all writes."""
    db = MagicMock()
    db.table.return_value.upsert.return_value.execute.return_value.data = []
    db.table.return_value.select.return_value.order.return_value.execute.return_value.data = []
    return db


def _valid_payload() -> dict:
    return {
        "email": "seller@example.com",
        "platform": "amazon",
        "product_count": 100,
        "reprice_frequency": "daily",
    }


# ---------------------------------------------------------------------------
# POST /beta/request
# ---------------------------------------------------------------------------


class TestSubmitBetaRequest:
    """POST /beta/request writes the row and returns 200."""

    def setup_method(self):
        app.dependency_overrides.clear()

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_successful_submission(self):
        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        with patch("api.routers.beta._send_beta_emails"):
            resp = client.post("/beta/request", json=_valid_payload())
        assert resp.status_code == 200
        body = resp.json()
        assert "waitlist" in body["message"].lower() or "list" in body["message"].lower()

    def test_duplicate_email_returns_200(self):
        """Upsert on duplicate email — idempotent, still 200."""
        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        with patch("api.routers.beta._send_beta_emails"):
            resp1 = client.post("/beta/request", json=_valid_payload())
            resp2 = client.post("/beta/request", json=_valid_payload())
        assert resp1.status_code == 200
        assert resp2.status_code == 200

    def test_invalid_email_returns_422(self):
        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        payload = {**_valid_payload(), "email": "not-an-email"}
        resp = client.post("/beta/request", json=payload)
        assert resp.status_code == 422

    def test_email_failure_does_not_fail_endpoint(self):
        """Email send error must not return a 5xx."""
        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        with patch(
            "api.routers.beta._send_beta_emails",
            side_effect=RuntimeError("email broken"),
        ):
            resp = client.post("/beta/request", json=_valid_payload())
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /beta/requests
# ---------------------------------------------------------------------------


class TestListBetaRequests:
    """GET /beta/requests is operator-only."""

    _SECRET = "correct-operator-secret-abc123"

    def setup_method(self):
        app.dependency_overrides.clear()

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_correct_secret_returns_200(self):
        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        with patch.dict(os.environ, {"OPERATOR_SECRET": self._SECRET}):
            resp = client.get(
                "/beta/requests",
                headers={"X-Operator-Secret": self._SECRET},
            )
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_wrong_secret_returns_403(self):
        with patch.dict(os.environ, {"OPERATOR_SECRET": self._SECRET}):
            resp = client.get(
                "/beta/requests",
                headers={"X-Operator-Secret": "wrong-secret"},
            )
        assert resp.status_code == 403

    def test_missing_secret_returns_403(self):
        with patch.dict(os.environ, {"OPERATOR_SECRET": self._SECRET}):
            resp = client.get("/beta/requests")
        assert resp.status_code == 403

    def test_operator_email_not_in_response(self):
        """OPERATOR_EMAIL must never appear in any API response body."""
        secret = "test-secret-xyz"
        operator_email = "operator@internal.com"
        db = _mock_db()
        app.dependency_overrides[get_db] = lambda: db
        with patch.dict(
            os.environ,
            {"OPERATOR_SECRET": secret, "OPERATOR_EMAIL": operator_email},
        ):
            resp = client.get(
                "/beta/requests",
                headers={"X-Operator-Secret": secret},
            )
        assert operator_email not in resp.text
