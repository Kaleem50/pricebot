"""
api/routers/billing.py — Billing and Subscription Endpoints

Manages Stripe subscriptions and exposes billing data to the dashboard.

Security invariants (SECURITY.md §5):
  - POST /billing/webhook is PUBLIC (no JWT) — auth is Stripe HMAC signature.
  - HMAC verification MUST succeed before any DB operation.
  - Tier is ALWAYS read from the subscriptions table — never from the client.
  - All subscription events are idempotent (safe to receive same event twice).
  - user_id in DB is linked via stripe_customer_id — never trust client metadata.

Endpoints:
  GET  /billing/subscription   Current plan, status, usage, next billing date
  POST /billing/portal         Create Stripe Customer Portal session URL
  POST /billing/webhook        Stripe webhook handler (HMAC-verified, public)

Stripe events handled:
  customer.subscription.created  → create/activate subscriptions row
  customer.subscription.updated  → update tier and status
  customer.subscription.deleted  → set status = 'canceled'
  invoice.payment_failed         → set status = 'past_due'
  invoice.payment_succeeded      → set status = 'active' (if was past_due)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from supabase import Client

from api.dependencies import AuthenticatedUser, get_current_user, get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

# Stripe tier mapping: Price ID → tier name
# Configured in environment to avoid hard-coding Stripe Price IDs in source
_PRICE_TO_TIER: dict[str, str] = {}


def _get_stripe_key() -> str:
    """Return the Stripe secret key from environment."""
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        raise RuntimeError("STRIPE_SECRET_KEY is not set")
    return key


def _get_webhook_secret() -> str:
    """Return the Stripe webhook signing secret from environment."""
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET is not set")
    return secret


def _price_id_to_tier(price_id: str) -> str:
    """
    Map a Stripe Price ID to a PriceBot tier name.

    Checks environment variables for the Price ID → tier mapping.
    Falls back to 'starter' if the price ID is not recognised.
    """
    if not _PRICE_TO_TIER:
        _PRICE_TO_TIER.update({
            os.environ.get("STRIPE_STARTER_PRICE_ID", ""): "starter",
            os.environ.get("STRIPE_GROWTH_PRICE_ID", ""): "growth",
            os.environ.get("STRIPE_PRO_PRICE_ID", ""): "pro",
        })
        _PRICE_TO_TIER.pop("", None)  # Remove empty-string key if env var not set

    return _PRICE_TO_TIER.get(price_id, "starter")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class SubscriptionResponse(BaseModel):
    """Current subscription state for the billing dashboard."""

    tier: str
    status: str
    current_period_end: datetime | None
    stripe_customer_id: str | None
    product_count: int
    overage_units: int


class PortalResponse(BaseModel):
    """Stripe Customer Portal session URL."""

    url: str


# ---------------------------------------------------------------------------
# GET /billing/subscription
# ---------------------------------------------------------------------------


@router.get("/subscription", response_model=SubscriptionResponse)
async def get_subscription(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: Client = Depends(get_db),
) -> SubscriptionResponse:
    """
    Return the current subscription details for the authenticated user.

    Data is read from the subscriptions table (written by the webhook handler).
    If no subscription row exists, returns a default Starter representation.
    """
    try:
        result = (
            db.table("subscriptions")
            .select(
                "tier, status, current_period_end, stripe_customer_id, "
                "product_count, overage_units"
            )
            .eq("user_id", current_user.id)
            .execute()
        )
    except Exception as exc:
        logger.error(
            "Failed to fetch subscription",
            extra={"user_id": current_user.id, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Failed to retrieve subscription")

    if not result.data:
        return SubscriptionResponse(
            tier="starter",
            status="active",
            current_period_end=None,
            stripe_customer_id=None,
            product_count=0,
            overage_units=0,
        )

    row = result.data[0]
    return SubscriptionResponse(
        tier=row["tier"],
        status=row["status"],
        current_period_end=row.get("current_period_end"),
        stripe_customer_id=row.get("stripe_customer_id"),
        product_count=row.get("product_count") or 0,
        overage_units=row.get("overage_units") or 0,
    )


# ---------------------------------------------------------------------------
# POST /billing/portal
# ---------------------------------------------------------------------------


@router.post("/portal", response_model=PortalResponse)
async def create_portal_session(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: Client = Depends(get_db),
) -> PortalResponse:
    """
    Create a Stripe Customer Portal session and return the redirect URL.

    Requires the user to have an active Stripe customer record.
    """
    try:
        result = (
            db.table("subscriptions")
            .select("stripe_customer_id")
            .eq("user_id", current_user.id)
            .execute()
        )
    except Exception as exc:
        logger.error(
            "DB error fetching stripe_customer_id for portal",
            extra={"user_id": current_user.id, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Database error")

    if not result.data or not result.data[0].get("stripe_customer_id"):
        raise HTTPException(
            status_code=404,
            detail="No billing account found. Subscribe to a plan first.",
        )

    customer_id = result.data[0]["stripe_customer_id"]
    return_url = os.environ.get("FRONTEND_URL", "http://localhost:3000") + "/dashboard/billing"

    try:
        stripe.api_key = _get_stripe_key()
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=return_url,
        )
    except stripe.StripeError as exc:
        logger.error(
            "Stripe portal session creation failed",
            extra={"user_id": current_user.id, "error": str(exc)},
        )
        raise HTTPException(status_code=502, detail=f"Stripe error: {exc}")
    except RuntimeError as exc:
        logger.critical("Stripe not configured", extra={"error": str(exc)})
        raise HTTPException(status_code=500, detail="Billing service not configured")

    return PortalResponse(url=session.url)


# ---------------------------------------------------------------------------
# POST /billing/webhook  (PUBLIC — no JWT)
# ---------------------------------------------------------------------------


@router.post("/webhook", status_code=200)
async def stripe_webhook(
    request: Request,
    db: Client = Depends(get_db),
) -> dict:
    """
    Handle Stripe webhook events.

    SECURITY: This endpoint is PUBLIC (no JWT).  The HMAC signature MUST be
    verified before any DB operation.  An invalid signature returns HTTP 400
    and is logged at CRITICAL level.

    All handlers are idempotent — receiving the same event twice is safe.
    Idempotency key: stripe_sub_id (unique constraint in subscriptions table).
    """
    payload = await request.body()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        webhook_secret = _get_webhook_secret()
        stripe.api_key = _get_stripe_key()
    except RuntimeError as exc:
        logger.critical("Stripe webhook secret not configured", extra={"error": str(exc)})
        raise HTTPException(status_code=500, detail="Webhook service not configured")

    # HMAC verification MUST happen first — before any DB operation
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except ValueError:
        logger.critical(
            "Stripe webhook: invalid payload (not JSON)",
            extra={"sig_header_present": bool(sig_header)},
        )
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.SignatureVerificationError:
        logger.critical(
            "Stripe webhook: HMAC signature verification FAILED — possible replay attack",
            extra={"sig_header": sig_header[:80] if sig_header else ""},
        )
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_type: str = event["type"]
    event_id: str = event["id"]

    logger.info(
        "Stripe webhook received",
        extra={"event_type": event_type, "event_id": event_id},
    )

    try:
        if event_type == "customer.subscription.created":
            await _handle_subscription_created(event, db)
        elif event_type == "customer.subscription.updated":
            await _handle_subscription_updated(event, db)
        elif event_type == "customer.subscription.deleted":
            await _handle_subscription_deleted(event, db)
        elif event_type == "invoice.payment_failed":
            await _handle_invoice_payment_failed(event, db)
        elif event_type == "invoice.payment_succeeded":
            await _handle_invoice_payment_succeeded(event, db)
        else:
            logger.info(
                "Stripe webhook: unhandled event type",
                extra={"event_type": event_type, "event_id": event_id},
            )
    except Exception as exc:
        logger.error(
            "Stripe webhook handler error",
            extra={"event_type": event_type, "event_id": event_id, "error": str(exc)},
        )
        # Return 200 to prevent Stripe from retrying — the error is logged for investigation
        return {"status": "error", "event_id": event_id}

    return {"status": "ok", "event_id": event_id}


# ---------------------------------------------------------------------------
# Webhook event handlers (private)
# ---------------------------------------------------------------------------


async def _get_user_id_from_customer(customer_id: str) -> str | None:
    """
    Retrieve user_id from Stripe customer metadata.

    Calls Stripe API to fetch the Customer object and reads metadata.user_id.
    This mapping is set when the customer is created during checkout.

    Returns None if the customer has no user_id in metadata.
    """
    try:
        customer = stripe.Customer.retrieve(customer_id)
        return customer.get("metadata", {}).get("user_id")
    except stripe.StripeError as exc:
        logger.error(
            "Failed to retrieve Stripe customer",
            extra={"customer_id": customer_id, "error": str(exc)},
        )
        return None


async def _handle_subscription_created(event: dict, db: Client) -> None:
    """
    Handle customer.subscription.created — create or activate subscriptions row.

    Idempotent: upserts on stripe_sub_id so duplicate events are safe.
    """
    sub = event["data"]["object"]
    sub_id: str = sub["id"]
    customer_id: str = sub["customer"]
    status: str = sub["status"]
    period_end: datetime = datetime.fromtimestamp(sub["current_period_end"], tz=timezone.utc)

    # Determine tier from the subscription's first price item
    price_id = ""
    items = sub.get("items", {}).get("data", [])
    if items:
        price_id = items[0].get("price", {}).get("id", "")
    tier = _price_id_to_tier(price_id)

    # Get user_id via Stripe customer metadata
    user_id = await _get_user_id_from_customer(customer_id)
    if not user_id:
        logger.error(
            "Cannot link subscription: Stripe customer has no user_id in metadata",
            extra={"stripe_sub_id": sub_id, "customer_id": customer_id},
        )
        return

    row = {
        "user_id": user_id,
        "stripe_customer_id": customer_id,
        "stripe_sub_id": sub_id,
        "tier": tier,
        "status": status,
        "current_period_end": period_end.isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    db.table("subscriptions").upsert(row, on_conflict="stripe_sub_id").execute()

    logger.info(
        "Subscription created",
        extra={
            "user_id": user_id,
            "stripe_sub_id": sub_id,
            "tier": tier,
            "status": status,
        },
    )


async def _handle_subscription_updated(event: dict, db: Client) -> None:
    """
    Handle customer.subscription.updated — update tier and status.

    Idempotent: updates based on stripe_sub_id.
    """
    sub = event["data"]["object"]
    sub_id: str = sub["id"]
    status: str = sub["status"]
    period_end: datetime = datetime.fromtimestamp(sub["current_period_end"], tz=timezone.utc)

    price_id = ""
    items = sub.get("items", {}).get("data", [])
    if items:
        price_id = items[0].get("price", {}).get("id", "")
    tier = _price_id_to_tier(price_id)

    db.table("subscriptions").update(
        {
            "tier": tier,
            "status": status,
            "current_period_end": period_end.isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("stripe_sub_id", sub_id).execute()

    logger.info(
        "Subscription updated",
        extra={"stripe_sub_id": sub_id, "tier": tier, "status": status},
    )


async def _handle_subscription_deleted(event: dict, db: Client) -> None:
    """
    Handle customer.subscription.deleted — mark as canceled.

    Idempotent: same operation run twice leaves status = 'canceled'.
    """
    sub = event["data"]["object"]
    sub_id: str = sub["id"]

    db.table("subscriptions").update(
        {
            "status": "canceled",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("stripe_sub_id", sub_id).execute()

    logger.info("Subscription canceled", extra={"stripe_sub_id": sub_id})


async def _handle_invoice_payment_failed(event: dict, db: Client) -> None:
    """
    Handle invoice.payment_failed — set subscription status to past_due.

    Idempotent: setting status = 'past_due' twice has no additional effect.
    """
    invoice = event["data"]["object"]
    sub_id: str | None = invoice.get("subscription")
    if not sub_id:
        logger.warning(
            "invoice.payment_failed has no subscription ID",
            extra={"invoice_id": invoice.get("id")},
        )
        return

    db.table("subscriptions").update(
        {
            "status": "past_due",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("stripe_sub_id", sub_id).execute()

    logger.info("Invoice payment failed — subscription past_due", extra={"stripe_sub_id": sub_id})


async def _handle_invoice_payment_succeeded(event: dict, db: Client) -> None:
    """
    Handle invoice.payment_succeeded — restore subscription to active if was past_due.

    Only updates when current status is past_due to avoid overwriting trialing/canceled.
    Idempotent: updating active → active is a no-op.
    """
    invoice = event["data"]["object"]
    sub_id: str | None = invoice.get("subscription")
    if not sub_id:
        return

    db.table("subscriptions").update(
        {
            "status": "active",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("stripe_sub_id", sub_id).eq("status", "past_due").execute()

    logger.info("Invoice payment succeeded — subscription active", extra={"stripe_sub_id": sub_id})
