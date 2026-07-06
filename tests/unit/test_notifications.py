"""
tests/unit/test_notifications.py — Unit tests for core/notifications.py

Covers:
  - No-op when RESEND_API_KEY is unset
  - Debounce: skip if notifications_sent row exists within 1 hour
  - Successful send: correct subject for was_auto_applied=True/False
  - Non-2xx Resend response handled gracefully (returns False, no raise)
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from core.notifications import send_price_change_email


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_call_kwargs(
    *,
    was_auto_applied: bool = True,
    guardrail_applied: bool = False,
    db: object = None,
) -> dict:
    """Return minimal kwargs for send_price_change_email."""
    return dict(
        user_email="seller@example.com",
        product_title="Blue Widget",
        old_price=24.99,
        new_price=22.49,
        strategy="undercut",
        reasoning="Competitor dropped to $22.00; undercutting slightly.",
        confidence="high",
        was_auto_applied=was_auto_applied,
        guardrail_applied=guardrail_applied,
        product_id="prod-uuid-1234",
        user_id="user-uuid-5678",
        db=db,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNoApiKey:
    """send_price_change_email is a no-op when RESEND_API_KEY is not set."""

    def test_returns_false_when_api_key_missing(self):
        env = {k: v for k, v in os.environ.items() if k != "RESEND_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            result = send_price_change_email(**_make_call_kwargs())
        assert result is False

    def test_no_http_call_when_api_key_missing(self):
        env = {k: v for k, v in os.environ.items() if k != "RESEND_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            with patch("httpx.post") as mock_post:
                send_price_change_email(**_make_call_kwargs())
                mock_post.assert_not_called()


class TestDebounce:
    """Email is suppressed when notifications_sent has a recent row."""

    def test_returns_false_when_recently_notified(self):
        mock_db = MagicMock()
        # Simulate notifications_sent returning a row
        mock_db.table.return_value.select.return_value.eq.return_value \
            .eq.return_value.gte.return_value.limit.return_value \
            .execute.return_value.data = [{"id": "some-uuid"}]

        with patch.dict(os.environ, {"RESEND_API_KEY": "re_test_key"}):
            with patch("httpx.post") as mock_post:
                result = send_price_change_email(**_make_call_kwargs(db=mock_db))
                mock_post.assert_not_called()
        assert result is False


class TestSuccessfulSend:
    """Email sends correctly and returns True for both subject variants."""

    def _mock_db_no_recent(self) -> MagicMock:
        """Return a mock db that reports no recent notification."""
        mock_db = MagicMock()
        mock_db.table.return_value.select.return_value.eq.return_value \
            .eq.return_value.gte.return_value.limit.return_value \
            .execute.return_value.data = []
        return mock_db

    def _mock_resend_ok(self) -> MagicMock:
        """Return a mock httpx response with status_code 200."""
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.status_code = 200
        return mock_resp

    def test_auto_applied_subject(self):
        mock_db = self._mock_db_no_recent()
        with patch.dict(os.environ, {"RESEND_API_KEY": "re_test_key"}):
            with patch("httpx.post", return_value=self._mock_resend_ok()) as mock_post:
                result = send_price_change_email(
                    **_make_call_kwargs(was_auto_applied=True, db=mock_db)
                )
        assert result is True
        call_json = mock_post.call_args.kwargs["json"]
        assert "Price updated automatically" in call_json["subject"]

    def test_suggestion_ready_subject(self):
        mock_db = self._mock_db_no_recent()
        with patch.dict(os.environ, {"RESEND_API_KEY": "re_test_key"}):
            with patch("httpx.post", return_value=self._mock_resend_ok()) as mock_post:
                result = send_price_change_email(
                    **_make_call_kwargs(was_auto_applied=False, db=mock_db)
                )
        assert result is True
        call_json = mock_post.call_args.kwargs["json"]
        assert "New price suggestion ready" in call_json["subject"]

    def test_api_key_not_in_log_output(self, caplog):
        mock_db = self._mock_db_no_recent()
        with patch.dict(os.environ, {"RESEND_API_KEY": "re_super_secret_key"}):
            with patch("httpx.post", return_value=self._mock_resend_ok()):
                send_price_change_email(**_make_call_kwargs(db=mock_db))
        assert "re_super_secret_key" not in caplog.text


class TestResendFailure:
    """Non-2xx from Resend is handled gracefully — returns False, never raises."""

    def _mock_db_no_recent(self) -> MagicMock:
        mock_db = MagicMock()
        mock_db.table.return_value.select.return_value.eq.return_value \
            .eq.return_value.gte.return_value.limit.return_value \
            .execute.return_value.data = []
        return mock_db

    def test_returns_false_on_non_2xx(self):
        mock_resp = MagicMock()
        mock_resp.is_success = False
        mock_resp.status_code = 422

        mock_db = self._mock_db_no_recent()
        with patch.dict(os.environ, {"RESEND_API_KEY": "re_test_key"}):
            with patch("httpx.post", return_value=mock_resp):
                result = send_price_change_email(**_make_call_kwargs(db=mock_db))
        assert result is False

    def test_returns_false_on_network_error(self):
        mock_db = self._mock_db_no_recent()
        with patch.dict(os.environ, {"RESEND_API_KEY": "re_test_key"}):
            with patch("httpx.post", side_effect=ConnectionError("timeout")):
                result = send_price_change_email(**_make_call_kwargs(db=mock_db))
        assert result is False
