"""
core/alerting.py — CRITICAL Log Alerting

Intercepts CRITICAL-level log records and sends alert emails to the operator
via the Resend API.  Installs as a standard ``logging.Handler`` so no existing
call sites need to change — any ``logger.critical(...)`` anywhere in the
codebase automatically triggers an alert.

Rate limiting:
  Max 1 alert email per error_type per 10 minutes, enforced in-process via
  an in-memory dict.  Prevents alert storms when a systemic failure generates
  many CRITICAL logs in rapid succession (e.g. a broken batch cycle failing
  100 products in a row).

Security:
  - RESEND_API_KEY and OPERATOR_EMAIL are never logged.
  - Alert failures are logged at WARNING (not CRITICAL) to avoid infinite loops.

Usage::

    # In api/main.py or workers/scheduler.py at startup:
    from core.alerting import install_alerting_handler
    install_alerting_handler()
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_RESEND_SEND_URL = "https://api.resend.com/emails"

# In-process rate-limit state: error_type → epoch seconds of last alert sent
_last_alert_sent: dict[str, float] = {}

# Minimum seconds between alerts for the same error_type
_ALERT_RATE_LIMIT_SECONDS = 600  # 10 minutes


# ---------------------------------------------------------------------------
# Alert email sender
# ---------------------------------------------------------------------------


def send_alert_email(
    *,
    error_type: str,
    message: str,
    product_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """
    Send an operator alert email for a CRITICAL system event.

    Rate limited to 1 per ``error_type`` per 10 minutes.  Never raises.

    Args:
        error_type: Short identifier for the error class
                    (e.g. 'guardrail_ceiling', 'claude_null', 'credential_decrypt').
        message:    Full error message.
        product_id: PriceBot product UUID if available.
        user_id:    Supabase user UUID if available.
    """
    api_key = os.environ.get("RESEND_API_KEY", "")
    operator_email = os.environ.get("OPERATOR_EMAIL", "")

    if not api_key or not operator_email:
        logger.warning(
            "RESEND_API_KEY or OPERATOR_EMAIL not set — cannot send alert",
            extra={"error_type": error_type},
        )
        return

    # Rate limit check
    now = time.time()
    last_sent = _last_alert_sent.get(error_type, 0.0)
    if now - last_sent < _ALERT_RATE_LIMIT_SECONDS:
        remaining = int(_ALERT_RATE_LIMIT_SECONDS - (now - last_sent))
        logger.warning(
            "Alert rate-limited — suppressing duplicate",
            extra={
                "error_type": error_type,
                "retry_in_seconds": remaining,
            },
        )
        return

    _last_alert_sent[error_type] = now

    timestamp = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ).isoformat()

    context_rows = ""
    if product_id:
        context_rows += f"<tr><td><strong>Product ID:</strong></td><td>{product_id}</td></tr>"
    if user_id:
        context_rows += f"<tr><td><strong>User ID:</strong></td><td>{user_id}</td></tr>"

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family: monospace; background: #fff; padding: 24px;">
  <h2 style="color: #dc2626;">⚠ PriceBot CRITICAL Alert</h2>
  <table cellpadding="6" cellspacing="0" style="border-collapse: collapse;">
    <tr><td><strong>Timestamp:</strong></td><td>{timestamp}</td></tr>
    <tr><td><strong>Error Type:</strong></td><td>{error_type}</td></tr>
    {context_rows}
    <tr>
      <td><strong>Message:</strong></td>
      <td style="white-space: pre-wrap; max-width: 480px;">{message}</td>
    </tr>
  </table>
  <p style="color: #6b7280; font-size: 12px; margin-top: 24px;">
    This alert was sent because a CRITICAL log was emitted.
    Rate-limited to 1 per error type per 10 minutes.
  </p>
</body>
</html>"""

    try:
        from_address = os.environ.get(
            "NOTIFICATIONS_FROM_EMAIL", "PriceBot Alerts <alerts@pricebot.io>"
        )
        response = httpx.post(
            _RESEND_SEND_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": from_address,
                "to": [operator_email],
                "subject": f"[PriceBot CRITICAL] {error_type}",
                "html": html,
            },
            timeout=8.0,
        )
        if not response.is_success:
            logger.warning(
                "Alert email delivery failed",
                extra={
                    "error_type": error_type,
                    "status_code": response.status_code,
                },
            )
    except Exception as exc:
        # Log at WARNING — never at CRITICAL (infinite loop risk)
        logger.warning(
            "Alert email send raised an exception",
            extra={"error_type": error_type, "error": str(exc)},
        )


# ---------------------------------------------------------------------------
# Logging handler
# ---------------------------------------------------------------------------


class CriticalAlertHandler(logging.Handler):
    """
    Logging handler that fires ``send_alert_email`` for every CRITICAL record.

    Installed once at application startup via ``install_alerting_handler()``.
    Ignores its own module's records to prevent any possibility of recursion.
    """

    def emit(self, record: logging.LogRecord) -> None:
        """
        Process a CRITICAL log record and send an alert email.

        Args:
            record: The log record emitted by any logger in the process.
        """
        if record.levelno < logging.CRITICAL:
            return

        # Prevent recursion — don't alert on logs from this module itself
        if record.name == __name__:
            return

        # Extract optional context fields stamped by structured logging
        extra: dict[str, Any] = {}
        for key in ("product_id", "user_id"):
            val = getattr(record, key, None)
            if val:
                extra[key] = str(val)

        # Derive a short, stable error_type from the log message
        error_type = _classify_error_type(record.getMessage())

        send_alert_email(
            error_type=error_type,
            message=record.getMessage(),
            product_id=extra.get("product_id"),
            user_id=extra.get("user_id"),
        )

    def handleError(self, record: logging.LogRecord) -> None:
        """Suppress handler errors — never let alerting crash the application."""
        pass  # intentional — alerting must never crash the app


def _classify_error_type(message: str) -> str:
    """
    Derive a short, stable error_type key from a CRITICAL log message.

    Used as the rate-limit key and alert subject.

    Args:
        message: The formatted log message string.

    Returns:
        A short snake_case identifier.
    """
    msg_lower = message.lower()
    if "ceiling" in msg_lower or "exceeds ceiling" in msg_lower:
        return "guardrail_ceiling"
    if "null result" in msg_lower or "null response" in msg_lower:
        return "claude_null_response"
    if "invalid price" in msg_lower:
        return "claude_invalid_price"
    if "decrypt" in msg_lower or "decryption" in msg_lower:
        return "credential_decrypt_failure"
    if "stripe" in msg_lower and "signature" in msg_lower:
        return "stripe_hmac_failure"
    if "tenant isolation" in msg_lower or "cross-tenant" in msg_lower:
        return "tenant_isolation_violation"
    if "mock" in msg_lower and "production" in msg_lower:
        return "mock_mode_in_production"
    # Fallback — first 40 chars, sanitised
    return message[:40].lower().replace(" ", "_").replace(":", "").rstrip("_")


# ---------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------


def install_alerting_handler() -> None:
    """
    Attach ``CriticalAlertHandler`` to the root logger.

    Idempotent — safe to call multiple times (subsequent calls are no-ops).
    Call once at application startup in ``api/main.py`` and ``workers/scheduler.py``.
    """
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, CriticalAlertHandler):
            return  # Already installed

    handler = CriticalAlertHandler(level=logging.CRITICAL)
    root.addHandler(handler)
    logger.info("CRITICAL alert handler installed")
