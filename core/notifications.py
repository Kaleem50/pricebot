"""
core/notifications.py — Transactional Email Notifications

Sends price-change emails to sellers via the Resend API.

Behaviour by tier:
  - Starter:    Subject "New price suggestion ready" — price NOT yet applied.
  - Growth/Pro: Subject "Price updated automatically" — price already applied.

Debounce:
  Never sends more than one email per product per hour.  Each sent email is
  recorded in the ``notifications_sent`` table and checked on every call.

Security constraints:
  - RESEND_API_KEY is never logged at any level.
  - Email failure never raises — logs ERROR and returns False.
  - The worker must wrap call in try/except regardless (belt-and-suspenders).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_RESEND_SEND_URL = "https://api.resend.com/emails"

# Plain-English descriptions for each AI strategy value
_STRATEGY_DESCRIPTIONS: dict[str, str] = {
    "undercut": "Slightly undercut competitors to improve sales rank",
    "match": "Match the lowest competitor price to stay competitive",
    "premium": "Hold a premium price — your listing quality justifies it",
    "hold": "Keep the current price — conditions don't warrant a change",
}

_CONFIDENCE_LABELS: dict[str, str] = {
    "high": "High",
    "medium": "Medium",
    "low": "Low",
}


# ---------------------------------------------------------------------------
# Debounce helpers
# ---------------------------------------------------------------------------


def _was_recently_notified(
    db: Any,
    user_id: str,
    product_id: str,
) -> bool:
    """
    Return True if an email was already sent for this product in the last hour.

    Args:
        db:         Supabase client.
        user_id:    Owner user ID.
        product_id: Product UUID.

    Returns:
        True if a notification was sent within the last 60 minutes.
    """
    if db is None:
        return False
    try:
        from datetime import timedelta

        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()

        result = (
            db.table("notifications_sent")
            .select("id")
            .eq("user_id", user_id)
            .eq("product_id", product_id)
            .gte("sent_at", cutoff)
            .limit(1)
            .execute()
        )
        return bool(result.data)
    except Exception as exc:
        logger.warning(
            "Failed to check email debounce — proceeding with send",
            extra={"user_id": user_id, "product_id": product_id, "error": str(exc)},
        )
        return False


def _record_notification_sent(
    db: Any,
    user_id: str,
    product_id: str,
    email_type: str,
) -> None:
    """
    Record a sent notification in the notifications_sent table for debounce.

    Silently ignores DB errors — a tracking failure must not block the pipeline.

    Args:
        db:         Supabase client.
        user_id:    Owner user ID.
        product_id: Product UUID.
        email_type: 'suggestion_ready' or 'auto_applied'.
    """
    if db is None:
        return
    try:
        db.table("notifications_sent").insert(
            {
                "user_id": user_id,
                "product_id": product_id,
                "email_type": email_type,
                "sent_at": datetime.now(timezone.utc).isoformat(),
            }
        ).execute()
    except Exception as exc:
        logger.warning(
            "Failed to record notification_sent — debounce may not work for next hour",
            extra={"user_id": user_id, "product_id": product_id, "error": str(exc)},
        )


# ---------------------------------------------------------------------------
# Email building
# ---------------------------------------------------------------------------


def _build_email_html(
    product_title: str,
    old_price: float,
    new_price: float,
    strategy: str,
    reasoning: str,
    confidence: str,
    was_auto_applied: bool,
    guardrail_applied: bool,
    product_id: str,
) -> str:
    """
    Build the HTML body for a price-change notification email.

    Args:
        product_title:    Product title as shown on the platform.
        old_price:        Previous listed price.
        new_price:        New or suggested price.
        strategy:         AI strategy: 'undercut', 'match', 'premium', 'hold'.
        reasoning:        AI one-sentence reasoning.
        confidence:       'high', 'medium', or 'low'.
        was_auto_applied: True if price was already pushed to the platform.
        guardrail_applied: True if the margin floor guardrail overrode Claude's price.
        product_id:       PriceBot product UUID for dashboard deep-link.

    Returns:
        HTML string for the email body.
    """
    frontend_url = os.environ.get("FRONTEND_URL", "https://pricebot.io")
    product_url = f"{frontend_url}/dashboard/products/{product_id}"

    strategy_desc = _STRATEGY_DESCRIPTIONS.get(
        strategy, strategy.replace("_", " ").capitalize()
    )
    confidence_label = _CONFIDENCE_LABELS.get(confidence.lower(), confidence.capitalize())

    price_direction = "↑" if new_price > old_price else "↓" if new_price < old_price else "→"
    action_text = "Updated to" if was_auto_applied else "Suggested price"

    guardrail_note = ""
    if guardrail_applied:
        guardrail_note = """
        <tr>
          <td style="padding: 8px 0; color: #059669; font-size: 14px;">
            ✓ Your margin floor was protected — the suggested price was adjusted
            to keep you above your minimum margin.
          </td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
             background: #f9fafb; margin: 0; padding: 32px 16px;">
  <table width="100%" cellpadding="0" cellspacing="0"
         style="max-width: 520px; margin: 0 auto;">
    <tr>
      <td style="padding-bottom: 24px;">
        <span style="font-size: 18px; font-weight: 700; color: #2563eb;">
          PriceBot
        </span>
      </td>
    </tr>
    <tr>
      <td style="background: #fff; border-radius: 12px; padding: 32px;
                 border: 1px solid #e5e7eb;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="font-size: 20px; font-weight: 600; color: #111827;
                       padding-bottom: 16px;">
              {product_title}
            </td>
          </tr>
          <tr>
            <td style="padding-bottom: 20px;">
              <span style="font-size: 28px; font-weight: 700; color: #111827;">
                ${old_price:.2f}
              </span>
              <span style="font-size: 24px; color: #6b7280; margin: 0 8px;">
                {price_direction}
              </span>
              <span style="font-size: 28px; font-weight: 700; color: #2563eb;">
                ${new_price:.2f}
              </span>
              <span style="font-size: 13px; color: #6b7280; display: block;
                           margin-top: 4px;">
                {action_text}
              </span>
            </td>
          </tr>
          <tr>
            <td style="background: #f3f4f6; border-radius: 8px; padding: 16px;
                       margin-bottom: 16px; font-size: 14px; color: #374151;">
              <strong>Strategy:</strong> {strategy_desc}<br>
              <strong>AI reasoning:</strong> {reasoning}<br>
              <strong>Confidence:</strong> {confidence_label}
            </td>
          </tr>
          {guardrail_note}
          <tr>
            <td style="padding-top: 20px;">
              <a href="{product_url}"
                 style="background: #2563eb; color: #fff; padding: 12px 24px;
                        border-radius: 8px; text-decoration: none; font-weight: 600;
                        font-size: 14px; display: inline-block;">
                View in dashboard
              </a>
            </td>
          </tr>
        </table>
      </td>
    </tr>
    <tr>
      <td style="padding-top: 16px; font-size: 12px; color: #9ca3af;
                 text-align: center;">
        PriceBot · You are receiving this because you have notifications enabled.
      </td>
    </tr>
  </table>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def send_price_change_email(
    *,
    user_email: str,
    product_title: str,
    old_price: float,
    new_price: float,
    strategy: str,
    reasoning: str,
    confidence: str,
    was_auto_applied: bool,
    guardrail_applied: bool,
    product_id: str,
    user_id: str,
    db: Any | None = None,
) -> bool:
    """
    Send a transactional price-change email via the Resend API.

    Behaviour:
      - Subject varies by tier: caller passes ``was_auto_applied`` to control it.
        Starter (was_auto_applied=False): "New price suggestion ready for {title}"
        Growth/Pro (was_auto_applied=True): "Price updated automatically for {title}"
      - Debounces to one email per product per hour via the notifications_sent table.
      - Never raises — logs ERROR on failure and returns False.

    Args:
        user_email:       Seller's email address.
        product_title:    Product title.
        old_price:        Previous price.
        new_price:        New or suggested price.
        strategy:         AI strategy ('undercut', 'match', 'premium', 'hold').
        reasoning:        AI reasoning sentence.
        confidence:       'high', 'medium', or 'low'.
        was_auto_applied: True if price was already pushed to the platform.
        guardrail_applied: True if margin floor guardrail overrode Claude's price.
        product_id:       PriceBot product UUID.
        user_id:          Supabase user UUID (for debounce tracking).
        db:               Optional Supabase client (required for debounce).

    Returns:
        True if email was sent successfully; False otherwise.
    """
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        logger.warning(
            "RESEND_API_KEY not set — skipping email notification",
            extra={"user_id": user_id, "product_id": product_id},
        )
        return False

    # Debounce check — skip if already notified within the last hour
    if _was_recently_notified(db, user_id, product_id):
        logger.info(
            "Email notification debounced — already sent for this product within 1 hour",
            extra={"user_id": user_id, "product_id": product_id},
        )
        return False

    from_address = os.environ.get(
        "NOTIFICATIONS_FROM_EMAIL", "PriceBot <notifications@pricebot.io>"
    )

    if was_auto_applied:
        subject = f"Price updated automatically: {product_title[:60]}"
        email_type = "auto_applied"
    else:
        subject = f"New price suggestion ready: {product_title[:60]}"
        email_type = "suggestion_ready"

    html_body = _build_email_html(
        product_title=product_title,
        old_price=old_price,
        new_price=new_price,
        strategy=strategy,
        reasoning=reasoning,
        confidence=confidence,
        was_auto_applied=was_auto_applied,
        guardrail_applied=guardrail_applied,
        product_id=product_id,
    )

    try:
        response = httpx.post(
            _RESEND_SEND_URL,
            headers={
                # api_key deliberately not logged — keep it out of any extra= dict
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": from_address,
                "to": [user_email],
                "subject": subject,
                "html": html_body,
            },
            timeout=10.0,
        )
    except Exception as exc:
        logger.error(
            "Email send failed — network or timeout error",
            extra={
                "user_id": user_id,
                "product_id": product_id,
                "error": str(exc),
            },
        )
        return False

    if not response.is_success:
        logger.error(
            "Resend API returned non-2xx status",
            extra={
                "user_id": user_id,
                "product_id": product_id,
                "status_code": response.status_code,
            },
        )
        return False

    logger.info(
        "Price-change email sent successfully",
        extra={
            "user_id": user_id,
            "product_id": product_id,
            "email_type": email_type,
            "was_auto_applied": was_auto_applied,
        },
    )

    _record_notification_sent(db, user_id, product_id, email_type)
    return True
