"""
api/routers/repricing.py — Repricing Job Endpoints

Endpoints:
  - POST /repricing/trigger-cycle  — (dev-only) Trigger batch submission immediately
  - GET  /repricing/history        — (planned) Paginated price change log
  - GET  /repricing/jobs           — (planned) Active + recent job states
  - POST /repricing/jobs/{id}/retry — (planned) Reset a FAILED job back to IDLE
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
import jwt

from api.dependencies import Tier, get_db
from core.repricing_engine import RepricingEngine
from workers.batch_submitter import BatchSubmitter
from supabase import Client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/repricing", tags=["repricing"])


class TriggerCycleResponse(BaseModel):
    """Response from trigger-cycle endpoint."""

    message: str
    batch_id: str | None = None
    product_count: int | None = None


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
        engine = RepricingEngine(api_key)
        submitter = BatchSubmitter(engine)
        result = submitter.submit_for_user(user_id, db, tier)
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
