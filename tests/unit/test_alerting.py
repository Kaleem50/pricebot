"""
tests/unit/test_alerting.py — Unit tests for core/alerting.py

Covers:
  - CriticalAlertHandler: CRITICAL record triggers send_alert_email
  - Rate limiting: second alert for same error_type within 10 minutes is suppressed
"""

from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock, call, patch

import pytest

from core.alerting import (
    CriticalAlertHandler,
    _last_alert_sent,
    install_alerting_handler,
    send_alert_email,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_log_record(
    message: str,
    level: int = logging.CRITICAL,
    name: str = "some.module",
) -> logging.LogRecord:
    """Create a LogRecord with the given message and level."""
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname="",
        lineno=0,
        msg=message,
        args=(),
        exc_info=None,
    )
    return record


# ---------------------------------------------------------------------------
# CriticalAlertHandler
# ---------------------------------------------------------------------------


class TestCriticalAlertHandler:
    """Handler fires send_alert_email for CRITICAL records and ignores others."""

    def setup_method(self):
        # Clear rate-limit state before each test
        _last_alert_sent.clear()

    def test_critical_record_triggers_alert(self):
        handler = CriticalAlertHandler(level=logging.CRITICAL)
        with patch("core.alerting.send_alert_email") as mock_send:
            handler.emit(_make_log_record("Guardrail triggered", logging.CRITICAL))
            mock_send.assert_called_once()

    def test_warning_record_does_not_trigger(self):
        handler = CriticalAlertHandler(level=logging.CRITICAL)
        with patch("core.alerting.send_alert_email") as mock_send:
            handler.emit(_make_log_record("Just a warning", logging.WARNING))
            mock_send.assert_not_called()

    def test_own_module_records_ignored(self):
        """Records from core.alerting itself must not trigger a send (infinite loop guard)."""
        handler = CriticalAlertHandler(level=logging.CRITICAL)
        with patch("core.alerting.send_alert_email") as mock_send:
            handler.emit(
                _make_log_record(
                    "Alert email send raised an exception",
                    logging.CRITICAL,
                    name="core.alerting",
                )
            )
            mock_send.assert_not_called()

    def test_handler_error_does_not_raise(self):
        """handleError must be a silent no-op — never crash the application."""
        handler = CriticalAlertHandler(level=logging.CRITICAL)
        # Should not raise
        handler.handleError(_make_log_record("anything"))


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TestAlertRateLimit:
    """Same error_type within 10 minutes is suppressed."""

    def setup_method(self):
        _last_alert_sent.clear()

    def test_first_call_sends(self):
        with patch.dict(
            "os.environ",
            {"RESEND_API_KEY": "re_test", "OPERATOR_EMAIL": "op@example.com"},
        ):
            with patch("httpx.post") as mock_post:
                mock_post.return_value = MagicMock(is_success=True, status_code=200)
                send_alert_email(error_type="guardrail_ceiling", message="test")
                mock_post.assert_called_once()

    def test_second_call_within_window_suppressed(self):
        _last_alert_sent["guardrail_ceiling"] = time.time()  # pretend just sent
        with patch.dict(
            "os.environ",
            {"RESEND_API_KEY": "re_test", "OPERATOR_EMAIL": "op@example.com"},
        ):
            with patch("httpx.post") as mock_post:
                send_alert_email(error_type="guardrail_ceiling", message="duplicate")
                mock_post.assert_not_called()

    def test_different_error_types_not_suppressed(self):
        _last_alert_sent["guardrail_ceiling"] = time.time()
        with patch.dict(
            "os.environ",
            {"RESEND_API_KEY": "re_test", "OPERATOR_EMAIL": "op@example.com"},
        ):
            with patch("httpx.post") as mock_post:
                mock_post.return_value = MagicMock(is_success=True, status_code=200)
                send_alert_email(error_type="claude_null_response", message="different type")
                mock_post.assert_called_once()
