"""
api/routers/repricing.py — Repricing Job Endpoints

Endpoints:
  - POST /repricing/trigger-cycle        — (dev-only) Trigger batch submission immediately
  - GET  /repricing/history              — Paginated price change audit log
  - GET  /repricing/jobs                 — (planned) Active + recent job states
  - POST /repricing/jobs/{id}/retry      — (planned) Reset a FAILED job back to IDLE

Security invariants (CLAUDE.md §5.4):
  - Every DB query on /history filters by current_user.id from the validated JWT.
  - user_id is never read from query params or request body.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from pydantic import BaseModel
import jwt

from api.dependencies import AuthenticatedUser, Tier, get_current_user, get_db
from workers.batch_submitter import BatchSubmitter
from supabase import Client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/repricing", tags=["repricing"])


class PriceHistoryEntry(BaseModel):
    """One price-change record from the price_history table, enriched with product title."""

    id: str
    product_title: str
    platform: str
    old_price: float
    new_price: float
    strategy: str | None
    confidence: int | None
    reasoning: str | None
    was_auto_applied: bool
    applied_at: datetime


class TriggerCycleResponse(BaseModel):
    """Response from trigger-cycle endpoint."""

    message: str
    batch_id: str | None = None
    product_count: int | None = None


@router.get("/history", response_model=list[PriceHistoryEntry])
async def get_repricing_history(
    limit: int = Query(default=50, ge=1, le=200, description="Max records to return"),
    offset: int = Query(default=0, ge=0, description="Records to skip for pagination"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: Client = Depends(get_db),
) -> list[PriceHistoryEntry]:
    """
    Return the authenticated user's price change history, newest first.

    Joins price_history with products to include the product title.
    Results are filtered strictly by user_id from the validated JWT —
    cross-tenant reads are impossible even if an offset is manipulated.

    Args:
        limit:  Maximum records to return (1–200, default 50).
        offset: Records to skip for cursor-style pagination (default 0).

    Returns:
        List of PriceHistoryEntry, ordered by applied_at DESC.

    Raises:
        HTTPException 500: On unexpected database errors.
    """
    logger.info(
        "Fetching price history",
        extra={"user_id": current_user.id, "limit": limit, "offset": offset},
    )

    try:
        result = (
            db.table("price_history")
            .select(
                "id, platform, old_price, new_price, strategy, confidence, "
                "reasoning, was_auto_applied, applied_at, products(title)"
            )
            .eq("user_id", current_user.id)
            .order("applied_at", desc=True)
            .limit(limit)
            .offset(offset)
            .execute()
        )
    except Exception as exc:
        logger.error(
            "Failed to fetch price history",
            extra={"user_id": current_user.id, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Failed to fetch price history")

    entries: list[PriceHistoryEntry] = []
    for row in result.data or []:
        product_info = row.get("products") or {}
        product_title = product_info.get("title", "Unknown product") if isinstance(product_info, dict) else "Unknown product"
        entries.append(
            PriceHistoryEntry(
                id=row["id"],
                product_title=product_title,
                platform=row["platform"],
                old_price=float(row["old_price"]),
                new_price=float(row["new_price"]),
                strategy=row.get("strategy"),
                confidence=row.get("confidence"),
                reasoning=row.get("reasoning"),
                was_auto_applied=bool(row.get("was_auto_applied", False)),
                applied_at=row["applied_at"],
            )
        )

    logger.info(
        "Price history fetched",
        extra={"user_id": current_user.id, "count": len(entries)},
    )
    return entries


@router.post("/trigger-cycle", response_model=TriggerCycleResponse, status_code=202)
async def trigger_repricing_cycle(
    authorization: str = Header(...),
    db: Client = Depends(get_db),
) -> TriggerCycleResponse:
    """
    (Development Only) Trigger an immediate repricing batch submission.

    This endpoint bypasses the scheduler and submits a batch for the
    authenticated user immediately. Useful for testing the worker pipeline
    without waiting 15 minutes for the scheduler cycle.

    Only available in ENVIRONMENT=development. Returns 404 in production.

    Returns:
        TriggerCycleResponse with batch_id if submission succeeded, or
        a message if no products were ready for repricing.

    Raises:
        HTTPException 404: If not in development environment.
        HTTPException 401: If authentication header is invalid.
        HTTPException 500: If batch submission failed.
    """
    # Guard: only allowed in development
    if os.environ.get("ENVIRONMENT", "").lower() != "development":
        logger.warning("Trigger-cycle attempted in non-development environment")
        raise HTTPException(status_code=404, detail="Not found")

    # Extract user_id from Bearer token (dev-only, no verification needed)
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid Authorization header")

    token = authorization[len("Bearer "):]
    try:
        # Decode without verification for dev (Supabase token)
        payload = jwt.decode(token, options={"verify_signature": False})
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token: missing user_id")
    except Exception as exc:
        logger.error(f"Failed to decode token: {exc}")
        raise HTTPException(status_code=401, detail="Invalid token")

    # Fetch user's tier from subscriptions table
    try:
        sub_result = (
            db.table("subscriptions")
            .select("tier")
            .eq("user_id", user_id)
            .execute()
        )
        if not sub_result.data:
            raise HTTPException(status_code=403, detail="No subscription found")
        tier_str = sub_result.data[0].get("tier", "starter")
        tier = Tier.from_db(tier_str)
    except Exception as exc:
        logger.error(f"Failed to fetch subscription: {exc}")
        raise HTTPException(status_code=500, detail="Failed to fetch subscription")

    logger.info(
        "Manual repricing cycle triggered",
        extra={"user_id": user_id, "tier": tier.name},
    )

    try:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not configured")
        submitter = BatchSubmitter(anthropic_api_key=api_key)
        result = await submitter.submit_for_user(user_id, db, tier)
    except Exception as exc:
        logger.error(
            "Batch submission failed during trigger-cycle",
            extra={"user_id": user_id, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail=f"Batch submission failed: {str(exc)}")

    if result is None:
        return TriggerCycleResponse(
            message="No products ready for repricing (all non-IDLE or tier limit reached)"
        )

    return TriggerCycleResponse(
        message="Batch submitted successfully",
        batch_id=result["batch_id"],
        product_count=result["product_count"],
    )
