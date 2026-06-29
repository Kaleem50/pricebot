"""
api/routers/products.py — Product Catalog Endpoints

Manages the seller's product catalog: listing, detail view, settings updates,
and manual price application for Starter-tier users.

Security invariants (CLAUDE.md §5.3 + §5.4):
  - Every DB query filters by current_user.id — cross-tenant leak prevention.
  - user_id is always sourced from the validated JWT.
  - Fail-safe guardrail (max(ai_price, cost + floor)) is applied even in manual
    apply path as defence-in-depth — the suggestion already went through guardrails
    in the batch poller, but we re-check before any platform API call.

Endpoints:
  GET  /products                  Paginated, filterable product list
  GET  /products/{id}             Product detail + last AI suggestion
  PATCH /products/{id}/settings   Update min_margin_floor, is_tracking
  POST /products/{id}/apply       Starter only: manually apply pending suggestion
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from supabase import Client

from api.dependencies import AuthenticatedUser, Tier, get_current_user, get_db
from core.crypto import decrypt_credential
from platforms import get_connector
from platforms.exceptions import PlatformAuthError, PlatformError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/products", tags=["products"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class PriceSuggestion(BaseModel):
    """Last AI-generated pricing suggestion from price_history."""

    id: str
    suggested_price: float
    strategy: str | None
    confidence: int | None
    reasoning: str | None
    competitor_low: float | None
    was_auto_applied: bool
    applied_at: datetime


class ProductListItem(BaseModel):
    """Compact product representation for paginated list view."""

    id: str
    title: str
    platform: str
    platform_product_id: str
    current_price: float
    state: str
    is_tracking: bool
    last_repriced_at: datetime | None


class ProductDetail(BaseModel):
    """Full product detail including last AI suggestion."""

    id: str
    title: str
    platform: str
    platform_product_id: str
    platform_sku: str | None
    current_price: float
    cost: float | None
    min_margin_floor: float
    state: str
    is_tracking: bool
    last_repriced_at: datetime | None
    last_synced_at: datetime | None
    reprice_cycle_count: int
    fail_reason: str | None
    last_suggestion: PriceSuggestion | None


class UpdateSettingsRequest(BaseModel):
    """Request body for PATCH /products/{id}/settings."""

    min_margin_floor: float | None = None
    is_tracking: bool | None = None

    model_config = {"extra": "forbid"}


class ApplyPriceResponse(BaseModel):
    """Response after manually applying a pending price suggestion."""

    product_id: str
    previous_price: float
    applied_price: float
    strategy: str | None
    message: str


# ---------------------------------------------------------------------------
# GET /products
# ---------------------------------------------------------------------------


@router.get("", response_model=list[ProductListItem])
async def list_products(
    platform: str | None = Query(default=None, description="Filter by platform"),
    state: str | None = Query(default=None, description="Filter by state"),
    is_tracking: bool | None = Query(default=None, description="Filter by tracking status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: Client = Depends(get_db),
) -> list[dict]:
    """
    Return the seller's product catalog with optional filtering and pagination.

    All results are filtered by user_id from JWT — cross-tenant isolation enforced.
    """
    try:
        query = (
            db.table("products")
            .select(
                "id, title, platform, platform_product_id, current_price, "
                "state, is_tracking, last_repriced_at"
            )
            .eq("user_id", current_user.id)
            .order("title", desc=False)
            .range(offset, offset + limit - 1)
        )
        if platform is not None:
            query = query.eq("platform", platform)
        if state is not None:
            query = query.eq("state", state)
        if is_tracking is not None:
            query = query.eq("is_tracking", is_tracking)

        result = query.execute()
    except Exception as exc:
        logger.error(
            "Failed to list products",
            extra={"user_id": current_user.id, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Failed to retrieve products")

    return result.data


# ---------------------------------------------------------------------------
# GET /products/{id}
# ---------------------------------------------------------------------------


@router.get("/{product_id}", response_model=ProductDetail)
async def get_product(
    product_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: Client = Depends(get_db),
) -> dict:
    """
    Return full product detail including the most recent AI pricing suggestion.

    The user_id filter ensures users can only view their own products.
    """
    try:
        result = (
            db.table("products")
            .select("*")
            .eq("id", product_id)
            .eq("user_id", current_user.id)
            .execute()
        )
    except Exception as exc:
        logger.error(
            "DB error fetching product",
            extra={"user_id": current_user.id, "product_id": product_id, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Database error")

    if not result.data:
        raise HTTPException(status_code=404, detail="Product not found")

    product = result.data[0]

    # Fetch latest suggestion from price_history
    last_suggestion: dict | None = None
    try:
        ph_result = (
            db.table("price_history")
            .select(
                "id, new_price, strategy, confidence, reasoning, "
                "competitor_low, was_auto_applied, applied_at"
            )
            .eq("product_id", product_id)
            .eq("user_id", current_user.id)
            .order("applied_at", desc=True)
            .limit(1)
            .execute()
        )
        if ph_result.data:
            row = ph_result.data[0]
            last_suggestion = {
                "id": row["id"],
                "suggested_price": float(row["new_price"]),
                "strategy": row.get("strategy"),
                "confidence": row.get("confidence"),
                "reasoning": row.get("reasoning"),
                "competitor_low": float(row["competitor_low"]) if row.get("competitor_low") else None,
                "was_auto_applied": row.get("was_auto_applied", False),
                "applied_at": row["applied_at"],
            }
    except Exception as exc:
        logger.warning(
            "Could not fetch price history for product",
            extra={"product_id": product_id, "error": str(exc)},
        )

    product["last_suggestion"] = last_suggestion
    return product


# ---------------------------------------------------------------------------
# PATCH /products/{id}/settings
# ---------------------------------------------------------------------------


@router.patch("/{product_id}/settings", response_model=dict)
async def update_product_settings(
    product_id: str,
    body: UpdateSettingsRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: Client = Depends(get_db),
) -> dict:
    """
    Update margin floor and/or tracking status for a product.

    Only updates fields that are explicitly set in the request body.
    At least one field must be provided.
    """
    updates: dict = {}
    if body.min_margin_floor is not None:
        if body.min_margin_floor < 0:
            raise HTTPException(
                status_code=400, detail="min_margin_floor must be >= 0"
            )
        updates["min_margin_floor"] = body.min_margin_floor
    if body.is_tracking is not None:
        updates["is_tracking"] = body.is_tracking

    if not updates:
        raise HTTPException(
            status_code=400,
            detail="At least one field (min_margin_floor or is_tracking) must be provided",
        )

    # Verify product exists and belongs to user
    try:
        check = (
            db.table("products")
            .select("id")
            .eq("id", product_id)
            .eq("user_id", current_user.id)
            .execute()
        )
    except Exception as exc:
        logger.error(
            "DB error verifying product ownership",
            extra={"user_id": current_user.id, "product_id": product_id, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Database error")

    if not check.data:
        raise HTTPException(status_code=404, detail="Product not found")

    updates["updated_at"] = datetime.now(timezone.utc).isoformat()

    try:
        result = (
            db.table("products")
            .update(updates)
            .eq("id", product_id)
            .eq("user_id", current_user.id)
            .execute()
        )
    except Exception as exc:
        logger.error(
            "Failed to update product settings",
            extra={"user_id": current_user.id, "product_id": product_id, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Failed to update product settings")

    logger.info(
        "Product settings updated",
        extra={
            "user_id": current_user.id,
            "product_id": product_id,
            "updates": list(updates.keys()),
        },
    )

    return result.data[0] if result.data else {"id": product_id, **updates}


# ---------------------------------------------------------------------------
# POST /products/{id}/apply
# ---------------------------------------------------------------------------


@router.post("/{product_id}/apply", response_model=ApplyPriceResponse)
async def apply_pending_price(
    product_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: Client = Depends(get_db),
) -> ApplyPriceResponse:
    """
    Manually apply the latest pending AI price suggestion to the platform.

    Available to Starter-tier users only.  Growth and Pro tiers have prices
    applied automatically by the batch poller — they do not need this endpoint.

    Steps:
      1. Verify user is Starter tier (Growth/Pro auto-apply).
      2. Get the product and its latest unapplied suggestion.
      3. Re-apply fail-safe guardrail (defence-in-depth).
      4. Decrypt platform credentials and instantiate connector.
      5. Call connector.apply_price().
      6. Mark price_history row as was_auto_applied=True.
      7. Update product.current_price.
    """
    if current_user.tier != Tier.STARTER:
        raise HTTPException(
            status_code=403,
            detail=(
                "Manual price application is only available for Starter-tier accounts. "
                "Your tier auto-applies prices after each AI repricing cycle."
            ),
        )

    # Fetch product
    try:
        prod_result = (
            db.table("products")
            .select("*")
            .eq("id", product_id)
            .eq("user_id", current_user.id)
            .execute()
        )
    except Exception as exc:
        logger.error(
            "DB error fetching product for manual apply",
            extra={"user_id": current_user.id, "product_id": product_id, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Database error")

    if not prod_result.data:
        raise HTTPException(status_code=404, detail="Product not found")

    product_row = prod_result.data[0]
    platform = product_row["platform"]
    previous_price = float(product_row["current_price"])
    cost = float(product_row.get("cost") or 0)
    min_margin_floor = float(product_row.get("min_margin_floor") or 0)
    floor_price = cost + min_margin_floor

    # Fetch latest unapplied suggestion
    try:
        ph_result = (
            db.table("price_history")
            .select("id, new_price, strategy")
            .eq("product_id", product_id)
            .eq("user_id", current_user.id)
            .eq("was_auto_applied", False)
            .order("applied_at", desc=True)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        logger.error(
            "DB error fetching pending suggestion",
            extra={"user_id": current_user.id, "product_id": product_id, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Database error")

    if not ph_result.data:
        raise HTTPException(
            status_code=404,
            detail="No pending price suggestion found. Wait for the next AI repricing cycle.",
        )

    suggestion = ph_result.data[0]
    suggested_price = float(suggestion["new_price"])
    price_history_id = suggestion["id"]
    strategy = suggestion.get("strategy")

    # MANDATORY GUARDRAIL — applied even though batch poller already applied it
    final_price = max(suggested_price, floor_price)
    if final_price != suggested_price:
        logger.warning(
            "Guardrail applied during manual price apply",
            extra={
                "user_id": current_user.id,
                "product_id": product_id,
                "suggested_price": suggested_price,
                "floor_price": floor_price,
                "final_price": final_price,
            },
        )

    # Get platform credentials
    try:
        conn_result = (
            db.table("platform_connections")
            .select("encrypted_creds, is_active")
            .eq("user_id", current_user.id)
            .eq("platform", platform)
            .execute()
        )
    except Exception as exc:
        logger.error(
            "DB error fetching platform connection for apply",
            extra={"user_id": current_user.id, "platform": platform, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Database error")

    if not conn_result.data or not conn_result.data[0]["is_active"]:
        raise HTTPException(
            status_code=400,
            detail=f"No active {platform} connection. Reconnect your account to apply prices.",
        )

    try:
        creds_dict: dict[str, str] = json.loads(
            decrypt_credential(conn_result.data[0]["encrypted_creds"])
        )
    except Exception as exc:
        logger.critical(
            "Failed to decrypt platform credentials during manual apply",
            extra={"user_id": current_user.id, "platform": platform, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Credential decryption error")

    try:
        connector = get_connector(
            platform=platform, credentials=creds_dict, user_id=current_user.id
        )
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc))

    # Build a minimal MyProduct for the connector
    from core.repricing_engine import MyProduct

    my_product = MyProduct(
        product_id=product_id,
        platform_product_id=product_row["platform_product_id"],
        platform_sku=product_row.get("platform_sku"),
        title=product_row["title"],
        platform=platform,
        current_price=previous_price,
        cost=cost,
        min_margin_floor=min_margin_floor,
        user_id=current_user.id,
    )

    try:
        await connector.apply_price(my_product, final_price)
    except PlatformAuthError:
        db.table("platform_connections").update({"is_active": False}).eq(
            "user_id", current_user.id
        ).eq("platform", platform).execute()
        raise HTTPException(
            status_code=401,
            detail="Platform credentials expired. Reconnect your account.",
        )
    except PlatformError as exc:
        logger.error(
            "Platform error during manual price apply",
            extra={
                "user_id": current_user.id,
                "product_id": product_id,
                "platform": platform,
                "error": str(exc),
            },
        )
        raise HTTPException(status_code=502, detail=f"Platform error: {exc}")

    now_iso = datetime.now(timezone.utc).isoformat()

    # Mark suggestion as applied
    try:
        db.table("price_history").update({"was_auto_applied": True}).eq(
            "id", price_history_id
        ).eq("user_id", current_user.id).execute()
    except Exception as exc:
        logger.warning(
            "Failed to mark price_history row as applied",
            extra={"price_history_id": price_history_id, "error": str(exc)},
        )

    # Update product current_price and state
    try:
        db.table("products").update(
            {
                "current_price": final_price,
                "state": "SYNCED",
                "last_repriced_at": now_iso,
                "updated_at": now_iso,
            }
        ).eq("id", product_id).eq("user_id", current_user.id).execute()
    except Exception as exc:
        logger.error(
            "Failed to update product price after manual apply",
            extra={"user_id": current_user.id, "product_id": product_id, "error": str(exc)},
        )

    logger.info(
        "Manual price apply successful",
        extra={
            "user_id": current_user.id,
            "product_id": product_id,
            "platform": platform,
            "previous_price": previous_price,
            "applied_price": final_price,
            "strategy": strategy,
        },
    )

    return ApplyPriceResponse(
        product_id=product_id,
        previous_price=previous_price,
        applied_price=final_price,
        strategy=strategy,
        message=f"Price updated from ${previous_price:.2f} to ${final_price:.2f}.",
    )
