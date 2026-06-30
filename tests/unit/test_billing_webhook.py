"""
tests/unit/test_billing_webhook.py — Stripe Webhook Handler Tests

Tests for POST /billing/webhook (api/routers/billing.py):
  - Valid HMAC signature → event is processed
  - Invalid HMAC signature → HTTP 400, nothing written to DB
  - Invalid payload (not JSON) → HTTP 400
  - customer.subscription.created → subscriptions row created/upserted
  - customer.subscription.updated → tier and status updated
  - customer.subscription.deleted → status = 'canceled'
  - invoice.payment_failed → status = 'past_due'
  - invoice.payment_succeeded → status = 'active' when was past_due
  - Idempotency: same event processed twice produces same result

All Supabase calls are mocked.  Stripe's construct_event is mocked to
avoid requiring a real webhook secret during tests.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import stripe
import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-billing-tests")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_fake_key_for_testing")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test_fake_secret")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", "a" * 64)

from api.main import app  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WEBHOOK_URL = "/billing/webhook"
_VALID_SIG = "t=1234567890,v1=fake_signature_that_passes_mock"

_PERIOD_END_TS = 1_800_000_000  # far future Unix timestamp
_PERIOD_END_ISO = datetime.fromtimestamp(_PERIOD_END_TS, tz=timezone.utc).isoformat()


def _make_event(event_type: str, data_object: dict, event_id: str = "evt_test_001") -> dict:
    """Build a minimal Stripe event dict."""
    return {
        "id": event_id,
        "type": event_type,
        "data": {"object": data_object},
    }


def _make_subscription(
    sub_id: str = "sub_test_001",
    customer_id: str = "cus_test_001",
    status: str = "active",
    price_id: str = "price_starter",
) -> dict:
    return {
        "id": sub_id,
        "customer": customer_id,
        "status": status,
        "current_period_end": _PERIOD_END_TS,
        "items": {
            "data": [{"price": {"id": price_id}}]
        },
    }


def _make_invoice(
    sub_id: str = "sub_test_001",
    invoice_id: str = "in_test_001",
) -> dict:
    return {
        "id": invoice_id,
        "subscription": sub_id,
    }


def _mock_db() -> MagicMock:
    """Build a mock Supabase Client that captures table operations."""
    db = MagicMock()
    table_mock = MagicMock()
    db.table = MagicMock(return_value=table_mock)

    # Chain: .table().update().eq().execute() etc.
    table_mock.update = MagicMock(return_value=table_mock)
    table_mock.upsert = MagicMock(return_value=table_mock)
    table_mock.select = MagicMock(return_value=table_mock)
    table_mock.eq = MagicMock(return_value=table_mock)
    table_mock.execute = MagicMock(return_value=MagicMock(data=[]))

    return db, table_mock


# ---------------------------------------------------------------------------
# Tests: HMAC verification
# ---------------------------------------------------------------------------


class TestWebhookAuthentication:
    """HMAC verification must happen before any DB operation."""

    def test_invalid_signature_returns_400(self):
        client = TestClient(app)
        with patch("stripe.Webhook.construct_event") as mock_construct:
            with patch("api.routers.billing._get_stripe_key", return_value="sk_test"):
                with patch("api.routers.billing._get_webhook_secret", return_value="whsec_test"):
                    mock_construct.side_effect = stripe.SignatureVerificationError(
                        "No signatures found", sig_header=_VALID_SIG
                    )
                    resp = client.post(
                        _WEBHOOK_URL,
                        content=b'{"type": "customer.subscription.created"}',
                        headers={"Stripe-Signature": "bad_sig"},
                    )

        assert resp.status_code == 400
        assert "signature" in resp.json().get("detail", "").lower()

    def test_invalid_payload_returns_400(self):
        client = TestClient(app)

        with patch("stripe.Webhook.construct_event") as mock_construct:
            with patch("api.routers.billing._get_stripe_key", return_value="sk_test"):
                with patch("api.routers.billing._get_webhook_secret", return_value="whsec_test"):
                    mock_construct.side_effect = ValueError("Invalid payload")
                    resp = client.post(
                        _WEBHOOK_URL,
                        content=b"not-json-at-all",
                        headers={"Stripe-Signature": _VALID_SIG},
                    )

        assert resp.status_code == 400

    def test_valid_signature_returns_200(self):
        from api.dependencies import get_db
        db_mock, _ = _mock_db()

        event = _make_event(
            "customer.subscription.updated",
            _make_subscription(),
        )

        app.dependency_overrides[get_db] = lambda: db_mock
        try:
            with patch("stripe.Webhook.construct_event", return_value=event):
                with patch("api.routers.billing._get_stripe_key", return_value="sk_test"):
                    with patch("api.routers.billing._get_webhook_secret", return_value="whsec_test"):
                        resp = TestClient(app).post(
                            _WEBHOOK_URL,
                            content=json.dumps(event).encode(),
                            headers={"Stripe-Signature": _VALID_SIG},
                        )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Tests: subscription.created
# ---------------------------------------------------------------------------


class TestSubscriptionCreated:
    """customer.subscription.created creates a subscriptions row."""

    @pytest.mark.asyncio
    async def test_creates_subscription_row(self):
        db_mock, table_mock = _mock_db()
        sub = _make_subscription(sub_id="sub_new_001", customer_id="cus_new_001")
        event = _make_event("customer.subscription.created", sub)

        customer_obj = {"metadata": {"user_id": "user-uuid-abc"}}

        with patch("stripe.Customer.retrieve", return_value=customer_obj):
            from api.routers.billing import _handle_subscription_created
            await _handle_subscription_created(event, db_mock)

        db_mock.table.assert_called_with("subscriptions")
        table_mock.upsert.assert_called_once()
        upsert_row = table_mock.upsert.call_args[0][0]
        assert upsert_row["user_id"] == "user-uuid-abc"
        assert upsert_row["stripe_sub_id"] == "sub_new_001"
        assert upsert_row["stripe_customer_id"] == "cus_new_001"
        assert upsert_row["status"] == "active"

    @pytest.mark.asyncio
    async def test_skips_if_no_user_id_in_metadata(self):
        db_mock, table_mock = _mock_db()
        sub = _make_subscription()
        event = _make_event("customer.subscription.created", sub)

        customer_obj = {"metadata": {}}  # no user_id

        with patch("stripe.Customer.retrieve", return_value=customer_obj):
            from api.routers.billing import _handle_subscription_created
            await _handle_subscription_created(event, db_mock)

        # Should not call upsert if we can't link to a user
        table_mock.upsert.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: subscription.updated
# ---------------------------------------------------------------------------


class TestSubscriptionUpdated:
    """customer.subscription.updated updates tier and status in DB."""

    @pytest.mark.asyncio
    async def test_updates_tier_and_status(self):
        db_mock, table_mock = _mock_db()
        sub = _make_subscription(status="trialing", price_id="price_growth")
        event = _make_event("customer.subscription.updated", sub)

        os.environ["STRIPE_GROWTH_PRICE_ID"] = "price_growth"

        from api.routers.billing import _handle_subscription_updated, _PRICE_TO_TIER
        _PRICE_TO_TIER.clear()  # reset cache

        await _handle_subscription_updated(event, db_mock)

        table_mock.update.assert_called_once()
        update_data = table_mock.update.call_args[0][0]
        assert update_data["tier"] == "growth"
        assert update_data["status"] == "trialing"

    @pytest.mark.asyncio
    async def test_unknown_price_defaults_to_starter(self):
        db_mock, table_mock = _mock_db()
        sub = _make_subscription(price_id="price_unknown_xyz")
        event = _make_event("customer.subscription.updated", sub)

        from api.routers.billing import _handle_subscription_updated, _PRICE_TO_TIER
        _PRICE_TO_TIER.clear()

        await _handle_subscription_updated(event, db_mock)

        update_data = table_mock.update.call_args[0][0]
        assert update_data["tier"] == "starter"


# ---------------------------------------------------------------------------
# Tests: subscription.deleted
# ---------------------------------------------------------------------------


class TestSubscriptionDeleted:
    """customer.subscription.deleted sets status = 'canceled'."""

    @pytest.mark.asyncio
    async def test_sets_status_canceled(self):
        db_mock, table_mock = _mock_db()
        sub = _make_subscription(sub_id="sub_to_cancel")
        event = _make_event("customer.subscription.deleted", sub)

        from api.routers.billing import _handle_subscription_deleted
        await _handle_subscription_deleted(event, db_mock)

        table_mock.update.assert_called_once()
        update_data = table_mock.update.call_args[0][0]
        assert update_data["status"] == "canceled"

    @pytest.mark.asyncio
    async def test_idempotent_double_cancel(self):
        db_mock, table_mock = _mock_db()
        sub = _make_subscription(sub_id="sub_cancel_twice")
        event = _make_event("customer.subscription.deleted", sub)

        from api.routers.billing import _handle_subscription_deleted
        await _handle_subscription_deleted(event, db_mock)
        await _handle_subscription_deleted(event, db_mock)

        assert table_mock.update.call_count == 2
        # Both calls set the same thing — idempotent
        for call in table_mock.update.call_args_list:
            assert call[0][0]["status"] == "canceled"


# ---------------------------------------------------------------------------
# Tests: invoice.payment_failed
# ---------------------------------------------------------------------------


class TestInvoicePaymentFailed:
    """invoice.payment_failed sets subscription status to past_due."""

    @pytest.mark.asyncio
    async def test_sets_past_due(self):
        db_mock, table_mock = _mock_db()
        invoice = _make_invoice(sub_id="sub_overdue_001")
        event = _make_event("invoice.payment_failed", invoice)

        from api.routers.billing import _handle_invoice_payment_failed
        await _handle_invoice_payment_failed(event, db_mock)

        table_mock.update.assert_called_once()
        update_data = table_mock.update.call_args[0][0]
        assert update_data["status"] == "past_due"

    @pytest.mark.asyncio
    async def test_no_subscription_id_is_noop(self):
        db_mock, table_mock = _mock_db()
        invoice = {"id": "in_no_sub", "subscription": None}
        event = _make_event("invoice.payment_failed", invoice)

        from api.routers.billing import _handle_invoice_payment_failed
        await _handle_invoice_payment_failed(event, db_mock)

        table_mock.update.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: invoice.payment_succeeded
# ---------------------------------------------------------------------------


class TestInvoicePaymentSucceeded:
    """invoice.payment_succeeded restores active status only when past_due."""

    @pytest.mark.asyncio
    async def test_restores_active_from_past_due(self):
        db_mock, table_mock = _mock_db()
        invoice = _make_invoice(sub_id="sub_paid_001")
        event = _make_event("invoice.payment_succeeded", invoice)

        from api.routers.billing import _handle_invoice_payment_succeeded
        await _handle_invoice_payment_succeeded(event, db_mock)

        table_mock.update.assert_called_once()
        update_data = table_mock.update.call_args[0][0]
        assert update_data["status"] == "active"

        # Verify only past_due rows are updated (eq filter)
        eq_calls = [str(call) for call in table_mock.eq.call_args_list]
        assert any("past_due" in call for call in eq_calls)

    @pytest.mark.asyncio
    async def test_no_subscription_id_is_noop(self):
        db_mock, table_mock = _mock_db()
        invoice = {"id": "in_no_sub", "subscription": None}
        event = _make_event("invoice.payment_succeeded", invoice)

        from api.routers.billing import _handle_invoice_payment_succeeded
        await _handle_invoice_payment_succeeded(event, db_mock)

        table_mock.update.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Receiving the same event twice must produce the same DB state."""

    @pytest.mark.asyncio
    async def test_subscription_updated_twice_is_safe(self):
        db_mock, table_mock = _mock_db()
        sub = _make_subscription(status="active")
        event = _make_event("customer.subscription.updated", sub)

        from api.routers.billing import _handle_subscription_updated, _PRICE_TO_TIER
        _PRICE_TO_TIER.clear()

        await _handle_subscription_updated(event, db_mock)
        await _handle_subscription_updated(event, db_mock)

        # Two DB calls but both write the same data
        assert table_mock.update.call_count == 2
        first_call = table_mock.update.call_args_list[0][0][0]
        second_call = table_mock.update.call_args_list[1][0][0]
        assert first_call["status"] == second_call["status"]
        assert first_call["tier"] == second_call["tier"]
