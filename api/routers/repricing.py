"""
api/routers/repricing.py — Repricing Job Endpoints

Endpoints:
  - POST /repricing/trigger-cycle  — (dev-only) Trigger batch submission immediately
  - GET  /repricing/history        — (planned) Paginated price change log
  - GET  /repricing/jobs           — (planned) Active + recent job states
  - POST /repricing/jobs/{id}/retry — (planned) Reset a FAILED job back to IDLE
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.dependencies import Tier, get_current_user, get_db
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
    current_user: Tier = Depends(get_current_user),
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
        HTTPException 500: If batch submission failed.
    """
    # Guard: only allowed in development
    if os.environ.get("ENVIRONMENT", "").lower() != "development":
        logger.warning(
            "Trigger-cycle attempted in non-development environment",
            extra={"user_id": current_user.id},
        )
        raise HTTPException(status_code=404, detail="Not found")

    logger.info(
        "Manual repricing cycle triggered",
        extra={"user_id": current_user.id, "tier": current_user.tier},
    )

    try:
        engine = RepricingEngine()
        submitter = BatchSubmitter(engine)
        result = submitter.submit_for_user(current_user.id, db, current_user.tier)
    except Exception as exc:
        logger.error(
            "Batch submission failed during trigger-cycle",
            extra={"user_id": current_user.id, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Batch submission failed")

    if result is None:
        return TriggerCycleResponse(
            message="No products ready for repricing (all non-IDLE or tier limit reached)"
        )

    return TriggerCycleResponse(
        message="Batch submitted successfully",
        batch_id=result["batch_id"],
        product_count=result["product_count"],
    )
