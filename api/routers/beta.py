"""
api/routers/beta.py — Beta Access Waitlist Endpoints

Two endpoints:

  POST /beta/request  (public — no auth required)
    Accepts a waitlist signup, stores in beta_requests table, sends
    confirmation email to the requester and a notification to the operator.

  GET /beta/requests  (operator only — OPERATOR_SECRET header required)
    Returns all beta requests sorted by created_at DESC.

Security:
  - POST /beta/request has no auth — deliberately public.
  - GET /beta/requests requires a constant-time comparison of the
    OPERATOR_SECRET header against the OPERATOR_SECRET env var.
  - OPERATOR_EMAIL is never returned in any API response.
  - beta_requests table has RLS: service role only (see 003_beta_requests.sql).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, EmailStr, Field
from supabase import Client

from api.dependencies import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/beta", tags=["beta"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class BetaRequest(BaseModel):
    """Payload for POST /beta/request."""

    email: EmailStr = Field(..., description="Requester's email address.")
    platform: Literal["amazon", "etsy", "shopify", "ebay", "woocommerce"] = Field(
        ..., description="Primary platform the seller uses."
    )
    product_count: int = Field(
        ..., ge=1, le=100_000, description="Approximate number of products."
    )
    reprice_frequency: Literal["daily", "weekly", "manual"] = Field(
        ..., description="How often the seller currently reprices."
    )


class BetaRequestResponse(BaseModel):
    """Response for POST /beta/request."""

    message: str


class BetaRequestRecord(BaseModel):
    """A single beta_requests row returned to the operator."""

    id: str
    email: str
    platform: str
    product_count: int
    reprice_frequency: str
    status: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Operator auth guard
# ---------------------------------------------------------------------------


def _require_operator(x_operator_secret: str = Header(default="")) -> None:
    """
    Validate the OPERATOR_SECRET header using constant-time comparison.

    Raises HTTP 403 if the header is missing or incorrect.
    """
    import hmac

    expected = os.environ.get("OPERATOR_SECRET", "")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Operator access is not configured on this server.",
        )
    if not hmac.compare_digest(x_operator_secret.encode(), expected.encode()):
        logger.warning("Operator endpoint accessed with invalid secret")
        raise HTTPException(status_code=403, detail="Forbidden")


# ---------------------------------------------------------------------------
# Email helpers (internal, use Resend via notifications pattern)
# ---------------------------------------------------------------------------


def _send_beta_emails(
    requester_email: str,
    platform: str,
    product_count: int,
    reprice_frequency: str,
) -> None:
    """
    Send confirmation email to requester and notification to OPERATOR_EMAIL.

    Never raises — logs ERROR on failure.

    Args:
        requester_email:  Email address of the waitlist signup.
        platform:         Platform they selected.
        product_count:    Number of products they manage.
        reprice_frequency: How often they currently reprice.
    """
    import httpx

    api_key = os.environ.get("RESEND_API_KEY", "")
    operator_email = os.environ.get("OPERATOR_EMAIL", "")
    frontend_url = os.environ.get("FRONTEND_URL", "https://pricebot.io")

    if not api_key:
        logger.warning(
            "RESEND_API_KEY not set — skipping beta confirmation emails",
            extra={"requester_email": requester_email},
        )
        return

    from_address = os.environ.get(
        "NOTIFICATIONS_FROM_EMAIL", "PriceBot <notifications@pricebot.io>"
    )

    # Confirmation to requester
    requester_html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
             background: #f9fafb; margin: 0; padding: 32px 16px;">
  <table width="100%" style="max-width: 520px; margin: 0 auto;">
    <tr><td style="padding-bottom: 16px;">
      <span style="font-size: 18px; font-weight: 700; color: #2563eb;">PriceBot</span>
    </td></tr>
    <tr>
      <td style="background: #fff; border-radius: 12px; padding: 32px;
                 border: 1px solid #e5e7eb;">
        <h2 style="font-size: 20px; color: #111827; margin-top: 0;">
          You're on the list!
        </h2>
        <p style="color: #374151;">
          Thanks for your interest in PriceBot. We'll be in touch within
          <strong>48 hours</strong> with your beta access details.
        </p>
        <p style="color: #374151;">
          In the meantime, you can learn more at
          <a href="{frontend_url}" style="color: #2563eb;">{frontend_url}</a>.
        </p>
        <p style="color: #6b7280; font-size: 13px; margin-top: 24px;">
          Platform: {platform} &nbsp;·&nbsp;
          Products: {product_count} &nbsp;·&nbsp;
          Repricing: {reprice_frequency}
        </p>
      </td>
    </tr>
  </table>
</body>
</html>"""

    try:
        httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": from_address,
                "to": [requester_email],
                "subject": "You're on the PriceBot beta waitlist",
                "html": requester_html,
            },
            timeout=8.0,
        )
    except Exception as exc:
        logger.error(
            "Failed to send beta confirmation email to requester",
            extra={"requester_email": requester_email, "error": str(exc)},
        )

    # Notification to operator
    if not operator_email:
        return

    operator_html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family: monospace; padding: 24px;">
  <h3>New PriceBot Beta Request</h3>
  <table cellpadding="6">
    <tr><td><b>Email:</b></td><td>{requester_email}</td></tr>
    <tr><td><b>Platform:</b></td><td>{platform}</td></tr>
    <tr><td><b>Products:</b></td><td>{product_count}</td></tr>
    <tr><td><b>Reprices:</b></td><td>{reprice_frequency}</td></tr>
    <tr><td><b>Time:</b></td><td>{datetime.now(timezone.utc).isoformat()}</td></tr>
  </table>
</body>
</html>"""

    try:
        httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": from_address,
                "to": [operator_email],
                "subject": f"New beta request: {requester_email} ({platform})",
                "html": operator_html,
            },
            timeout=8.0,
        )
    except Exception as exc:
        logger.error(
            "Failed to send beta notification email to operator",
            extra={"error": str(exc)},
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/request", response_model=BetaRequestResponse, status_code=200)
async def submit_beta_request(
    body: BetaRequest,
    db: Client = Depends(get_db),
) -> BetaRequestResponse:
    """
    Submit a beta access request.

    Public endpoint — no authentication required.

    Idempotent: if the same email has already submitted a request the row is
    upserted (no duplicate) and the response is still 200.

    Sends:
      1. Confirmation email to the requester.
      2. Notification email to OPERATOR_EMAIL.

    Args:
        body: Beta request payload.
        db:   Supabase client (injected).

    Returns:
        BetaRequestResponse with a confirmation message.
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        db.table("beta_requests").upsert(
            {
                "email": body.email,
                "platform": body.platform,
                "product_count": body.product_count,
                "reprice_frequency": body.reprice_frequency,
                "status": "pending",
                "created_at": now_iso,
                "updated_at": now_iso,
            },
            on_conflict="email",
        ).execute()
    except Exception as exc:
        logger.error(
            "Failed to store beta request",
            extra={"email": body.email, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Failed to record your request")

    logger.info(
        "Beta request submitted",
        extra={
            "email": body.email,
            "platform": body.platform,
            "product_count": body.product_count,
        },
    )

    # Fire-and-forget — email failure must not fail this endpoint
    try:
        _send_beta_emails(
            requester_email=body.email,
            platform=body.platform,
            product_count=body.product_count,
            reprice_frequency=body.reprice_frequency,
        )
    except Exception as exc:
        logger.error(
            "Beta email send raised unexpected error",
            extra={"email": body.email, "error": str(exc)},
        )

    return BetaRequestResponse(
        message=(
            "You're on the list — we'll be in touch within 48 hours."
        )
    )


@router.get(
    "/requests",
    response_model=list[BetaRequestRecord],
    dependencies=[Depends(_require_operator)],
)
async def list_beta_requests(
    db: Client = Depends(get_db),
) -> list[dict]:
    """
    Return all beta requests, newest first.

    Operator-only endpoint — requires ``X-Operator-Secret`` header matching
    the ``OPERATOR_SECRET`` environment variable.

    Args:
        db: Supabase client (injected).

    Returns:
        List of BetaRequestRecord objects, ordered by created_at DESC.

    Raises:
        HTTPException 403: If the operator secret header is missing or wrong.
        HTTPException 500: On DB error.
    """
    try:
        result = (
            db.table("beta_requests")
            .select("id, email, platform, product_count, reprice_frequency, status, created_at")
            .order("created_at", desc=True)
            .execute()
        )
    except Exception as exc:
        error_msg = str(exc)
        logger.error(
            "Failed to fetch beta requests",
            extra={
                "error": error_msg,
                "error_type": type(exc).__name__,
            },
        )
        # Return a more informative error for debugging
        detail_msg = error_msg[:200] if error_msg else "Unknown database error"
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve beta requests: {detail_msg}",
        )

    return result.data
