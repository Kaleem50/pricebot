"""
workers/batch_poller.py — Batch Result Poller

Polls Anthropic for completed batch results every 5 minutes, retrieves
parsed repricing recommendations, applies prices to platforms (for Growth/Pro
tiers), and records price history and usage events.

Execution model:
  - Called every 5 minutes by the scheduler.
  - Queries repricing_jobs WHERE state='BATCH_SUBMITTED'.
  - Extracts DISTINCT batch_ids and checks is_batch_complete() for each.
  - On completion: retrieves results, updates job state, optionally applies prices.
  - Writes price_history for every repriced product (Starter: suggestion only).
  - Starter tier: logs suggestions to price_history, does NOT call apply_price().
  - Growth/Pro: calls connector.apply_price() to update platform listing.
  - Records usage_event type='batch_completed' for cost tracking.

State transitions:
  BATCH_SUBMITTED → PROCESSING (temporary, during poll cycle)
  PROCESSING → SYNCED (on success, price applied or suggested)
  PROCESSING → FAILED (on error, platform unavailable, etc.)

Security constraints (CLAUDE.md §5.4 + SECURITY.md §3):
  - Every DB query filters by user_id.
  - Credentials decrypted in-memory only, never logged.
  - Tier check prevents apply_price() for Starter tier.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from supabase import Client

from api.dependencies import Tier
from core.crypto import decrypt_credential
from core.repricing_engine import RepricingEngine
from platforms import get_connector

logger = logging.getLogger(__name__)


class BatchPoller:
    """
    Polls Anthropic for batch completion and applies repricing recommendations.

    Typical usage (called by scheduler every 5 min)::

        poller = BatchPoller(anthropic_api_key=settings.ANTHROPIC_API_KEY)
        result = poller.poll_all_pending(db=db_client)

        logger.info("Poll cycle complete", extra=result)
    """

    def __init__(self, anthropic_api_key: str) -> None:
        """
        Initialise the batch poller with an Anthropic API key.

        Args:
            anthropic_api_key: Anthropic API key from ANTHROPIC_API_KEY env var.
                              Never logged or exposed.
        """
        if not anthropic_api_key:
            raise ValueError("anthropic_api_key must not be empty")
        self._engine = RepricingEngine(api_key=anthropic_api_key)

    def poll_all_pending(self, db: Client) -> dict[str, Any]:
        """
        Poll all pending batches for completion and process results.

        Flow:
          1. Query repricing_jobs WHERE state='BATCH_SUBMITTED'.
          2. Extract DISTINCT batch_ids and group jobs by batch_id.
          3. For each batch: check is_batch_complete().
          4. If complete: retrieve_batch_results().
          5. For each recommendation:
             - Fetch product tier from subscriptions table.
             - Starter: write price_history with was_auto_applied=False.
             - Growth/Pro: call connector.apply_price(), then write price_history.
          6. Update repricing_jobs state to SYNCED or FAILED.
          7. Record usage_event type='batch_completed'.

        Args:
            db: Supabase client.

        Returns:
            Dict with keys:
              - succeeded: int (products successfully processed)
              - failed: int (products failed or skipped)
              - batches_polled: int (number of batch IDs checked)
              - batches_completed: int (number that finished processing)
        """
        logger.info("Batch poller: querying BATCH_SUBMITTED jobs")

        try:
            submitted_result = (
                db.table("repricing_jobs")
                .select("id, user_id, product_id, batch_id, platform, anthropic_custom_id")
                .eq("state", "BATCH_SUBMITTED")
                .execute()
            )
        except Exception as exc:
            logger.error(
                "Failed to query BATCH_SUBMITTED jobs",
                extra={"error": str(exc)},
            )
            raise

        submitted_jobs = submitted_result.data or []
        if not submitted_jobs:
            logger.info("No BATCH_SUBMITTED jobs to poll")
            return {
                "succeeded": 0,
                "failed": 0,
                "batches_polled": 0,
                "batches_completed": 0,
            }

        # Group by batch_id
        jobs_by_batch: dict[str, list[dict]] = {}
        for job in submitted_jobs:
            batch_id = job.get("batch_id")
            if batch_id:
                if batch_id not in jobs_by_batch:
                    jobs_by_batch[batch_id] = []
                jobs_by_batch[batch_id].append(job)

        succeeded = 0
        failed = 0
        batches_completed = 0

        for batch_id, batch_jobs in jobs_by_batch.items():
            logger.info(
                "Checking batch status",
                extra={"batch_id": batch_id, "job_count": len(batch_jobs)},
            )

            # Check if batch is complete
            try:
                is_complete = self._engine.is_batch_complete(batch_id)
            except Exception as exc:
                logger.error(
                    "Failed to check batch status",
                    extra={"batch_id": batch_id, "error": str(exc)},
                )
                continue

            if not is_complete:
                logger.info(
                    "Batch still processing",
                    extra={"batch_id": batch_id},
                )
                continue

            logger.info(
                "Batch complete, retrieving results",
                extra={"batch_id": batch_id},
            )
            batches_completed += 1

            # Fetch products for guardrail checks
            product_ids = [j["product_id"] for j in batch_jobs]
            try:
                products_result = (
                    db.table("products")
                    .select(
                        "id, user_id, platform_product_id, title, current_price, cost, min_margin_floor, platform, platform_sku"
                    )
                    .in_("id", product_ids)
                    .execute()
                )
            except Exception as exc:
                logger.error(
                    "Failed to fetch products for batch",
                    extra={"batch_id": batch_id, "error": str(exc)},
                )
                continue

            products_by_id = {
                p["id"]: p for p in (products_result.data or [])
            }

            # Convert to MyProduct instances
            from core.repricing_engine import MyProduct

            my_products_by_id = {}
            for product_id, row in products_by_id.items():
                my_products_by_id[product_id] = MyProduct(
                    product_id=row["id"],
                    platform_product_id=row["platform_product_id"],
                    platform_sku=row.get("platform_sku"),
                    title=row["title"],
                    platform=row["platform"],
                    current_price=float(row["current_price"]),
                    cost=float(row.get("cost") or 0),
                    min_margin_floor=float(row.get("min_margin_floor") or 0),
                    user_id=row["user_id"],
                    platform_context={},
                    metadata={},
                )

            # Build custom_id → product_id mapping from persisted anthropic_custom_id values.
            # This is the inverse of the map created by submit_batch() and stored per-job.
            custom_id_to_product_id: dict[str, str] = {
                job["anthropic_custom_id"]: job["product_id"]
                for job in batch_jobs
                if job.get("anthropic_custom_id")
            }

            # Retrieve and parse batch results
            try:
                recommendations = self._engine.retrieve_batch_results(
                    batch_id=batch_id,
                    products_by_id=my_products_by_id,
                    custom_id_to_product_id=custom_id_to_product_id,
                )
            except Exception as exc:
                logger.error(
                    "Failed to retrieve batch results",
                    extra={"batch_id": batch_id, "error": str(exc)},
                )
                continue

            # Process each recommendation
            rec_by_product_id = {rec.product_id: rec for rec in recommendations}
            # Track which products succeed so we can mark the rest FAILED in products table.
            succeeded_product_ids: set[str] = set()

            for job in batch_jobs:
                product_id = job["product_id"]
                user_id = job["user_id"]
                platform = job["platform"]
                job_id = job["id"]

                if product_id not in my_products_by_id:
                    logger.error(
                        "Product not found for job",
                        extra={
                            "batch_id": batch_id,
                            "product_id": product_id,
                            "user_id": user_id,
                        },
                    )
                    failed += 1
                    try:
                        db.table("repricing_jobs").update(
                            {
                                "state": "FAILED",
                                "fail_reason": "Product not found",
                                "completed_at": datetime.now(timezone.utc).isoformat(),
                                "updated_at": datetime.now(timezone.utc).isoformat(),
                            }
                        ).eq("id", job_id).execute()
                    except Exception as exc:
                        logger.error(
                            "Failed to update job to FAILED",
                            extra={"job_id": job_id, "error": str(exc)},
                        )
                    continue

                product = my_products_by_id[product_id]

                # Check if recommendation was parsed successfully
                if product_id not in rec_by_product_id:
                    logger.warning(
                        "Product recommendation not in results (parse/guardrail failure)",
                        extra={
                            "batch_id": batch_id,
                            "product_id": product_id,
                            "user_id": user_id,
                        },
                    )
                    failed += 1
                    try:
                        db.table("repricing_jobs").update(
                            {
                                "state": "FAILED",
                                "fail_reason": "Claude response parse failed or guardrail triggered",
                                "completed_at": datetime.now(timezone.utc).isoformat(),
                                "updated_at": datetime.now(timezone.utc).isoformat(),
                            }
                        ).eq("id", job_id).execute()
                    except Exception as exc:
                        logger.error(
                            "Failed to update job to FAILED",
                            extra={"job_id": job_id, "error": str(exc)},
                        )
                    continue

                recommendation = rec_by_product_id[product_id]

                # Fetch user tier
                try:
                    sub_result = (
                        db.table("subscriptions")
                        .select("tier")
                        .eq("user_id", user_id)
                        .execute()
                    )
                except Exception as exc:
                    logger.error(
                        "Failed to fetch subscription tier",
                        extra={"user_id": user_id, "error": str(exc)},
                    )
                    failed += 1
                    try:
                        db.table("repricing_jobs").update(
                            {
                                "state": "FAILED",
                                "fail_reason": "Tier lookup failed",
                                "completed_at": datetime.now(timezone.utc).isoformat(),
                                "updated_at": datetime.now(timezone.utc).isoformat(),
                            }
                        ).eq("id", job_id).execute()
                    except Exception as inner_exc:
                        logger.error(
                            "Failed to update job to FAILED",
                            extra={"job_id": job_id, "error": str(inner_exc)},
                        )
                    continue

                tier_str = (
                    sub_result.data[0]["tier"]
                    if sub_result.data
                    else "starter"
                )
                try:
                    tier = Tier.from_db(tier_str)
                except ValueError:
                    tier = Tier.STARTER

                # For Growth/Pro: apply price to platform
                price_applied = False
                if tier in (Tier.GROWTH, Tier.PRO):
                    try:
                        cred_result = (
                            db.table("platform_connections")
                            .select("encrypted_creds")
                            .eq("user_id", user_id)
                            .eq("platform", platform)
                            .eq("is_active", True)
                            .execute()
                        )
                    except Exception as exc:
                        logger.error(
                            "Failed to fetch credentials for price apply",
                            extra={
                                "user_id": user_id,
                                "platform": platform,
                                "error": str(exc),
                            },
                        )
                        failed += 1
                        try:
                            db.table("repricing_jobs").update(
                                {
                                    "state": "FAILED",
                                    "fail_reason": "Credential lookup failed",
                                    "completed_at": datetime.now(timezone.utc).isoformat(),
                                    "updated_at": datetime.now(timezone.utc).isoformat(),
                                }
                            ).eq("id", job_id).execute()
                        except Exception as inner_exc:
                            logger.error(
                                "Failed to update job to FAILED",
                                extra={"job_id": job_id, "error": str(inner_exc)},
                            )
                        continue

                    if not cred_result.data:
                        logger.error(
                            "No active platform connection for price apply",
                            extra={
                                "user_id": user_id,
                                "platform": platform,
                            },
                        )
                        failed += 1
                        try:
                            db.table("repricing_jobs").update(
                                {
                                    "state": "FAILED",
                                    "fail_reason": "No active platform connection",
                                    "completed_at": datetime.now(timezone.utc).isoformat(),
                                    "updated_at": datetime.now(timezone.utc).isoformat(),
                                }
                            ).eq("id", job_id).execute()
                        except Exception as inner_exc:
                            logger.error(
                                "Failed to update job to FAILED",
                                extra={"job_id": job_id, "error": str(inner_exc)},
                            )
                        continue

                    encrypted_creds = cred_result.data[0]["encrypted_creds"]
                    try:
                        creds_json = decrypt_credential(encrypted_creds)
                        creds_dict = json.loads(creds_json)
                    except Exception as exc:
                        logger.critical(
                            "Failed to decrypt credentials",
                            extra={
                                "user_id": user_id,
                                "platform": platform,
                                "error": str(exc),
                            },
                        )
                        failed += 1
                        try:
                            db.table("repricing_jobs").update(
                                {
                                    "state": "FAILED",
                                    "fail_reason": "Credential decryption failed",
                                    "completed_at": datetime.now(timezone.utc).isoformat(),
                                    "updated_at": datetime.now(timezone.utc).isoformat(),
                                }
                            ).eq("id", job_id).execute()
                        except Exception as inner_exc:
                            logger.error(
                                "Failed to update job to FAILED",
                                extra={"job_id": job_id, "error": str(inner_exc)},
                            )
                        continue

                    try:
                        connector = get_connector(
                            platform=platform,
                            credentials=creds_dict,
                            user_id=user_id,
                            db=db,
                        )
                    except Exception as exc:
                        logger.error(
                            "Failed to instantiate connector for price apply",
                            extra={
                                "user_id": user_id,
                                "platform": platform,
                                "error": str(exc),
                            },
                        )
                        failed += 1
                        try:
                            db.table("repricing_jobs").update(
                                {
                                    "state": "FAILED",
                                    "fail_reason": "Connector instantiation failed",
                                    "completed_at": datetime.now(timezone.utc).isoformat(),
                                    "updated_at": datetime.now(timezone.utc).isoformat(),
                                }
                            ).eq("id", job_id).execute()
                        except Exception as inner_exc:
                            logger.error(
                                "Failed to update job to FAILED",
                                extra={"job_id": job_id, "error": str(inner_exc)},
                            )
                        continue

                    # Apply price (async)
                    try:
                        asyncio.run(
                            connector.apply_price(product, recommendation.final_price)
                        )
                        price_applied = True
                        logger.info(
                            "Price applied to platform",
                            extra={
                                "product_id": product_id,
                                "user_id": user_id,
                                "platform": platform,
                                "final_price": recommendation.final_price,
                            },
                        )
                    except Exception as exc:
                        logger.error(
                            "Failed to apply price to platform",
                            extra={
                                "product_id": product_id,
                                "user_id": user_id,
                                "platform": platform,
                                "error": str(exc),
                            },
                        )
                        failed += 1
                        try:
                            db.table("repricing_jobs").update(
                                {
                                    "state": "FAILED",
                                    "fail_reason": f"Price apply failed: {str(exc)[:100]}",
                                    "completed_at": datetime.now(timezone.utc).isoformat(),
                                    "updated_at": datetime.now(timezone.utc).isoformat(),
                                }
                            ).eq("id", job_id).execute()
                        except Exception as inner_exc:
                            logger.error(
                                "Failed to update job to FAILED",
                                extra={"job_id": job_id, "error": str(inner_exc)},
                            )
                        continue

                # Write price_history
                try:
                    db.table("price_history").insert(
                        {
                            "user_id": user_id,
                            "product_id": product_id,
                            "repricing_job_id": job_id,
                            "platform": platform,
                            "old_price": product.current_price,
                            "new_price": recommendation.final_price,
                            "strategy": recommendation.strategy,
                            "confidence": recommendation.confidence,
                            "reasoning": recommendation.reasoning,
                            "was_auto_applied": price_applied,
                            "competitor_low": recommendation.competitor_low,
                            "competitor_count": recommendation.competitor_count,
                        }
                    ).execute()
                except Exception as exc:
                    logger.error(
                        "Failed to write price_history",
                        extra={
                            "product_id": product_id,
                            "user_id": user_id,
                            "error": str(exc),
                        },
                    )

                # Update products table: apply new price and reset state to IDLE
                # so the scheduler picks this product up in the next cycle.
                try:
                    db.table("products").update(
                        {
                            "current_price": recommendation.final_price,
                            "state": "IDLE",
                            "last_repriced_at": datetime.now(timezone.utc).isoformat(),
                            "last_synced_at": datetime.now(timezone.utc).isoformat(),
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }
                    ).eq("id", product_id).eq("user_id", user_id).execute()
                except Exception as exc:
                    logger.error(
                        "Failed to update products table",
                        extra={
                            "product_id": product_id,
                            "user_id": user_id,
                            "error": str(exc),
                        },
                    )

                # Update repricing_jobs to SYNCED
                try:
                    db.table("repricing_jobs").update(
                        {
                            "state": "SYNCED",
                            "completed_at": datetime.now(timezone.utc).isoformat(),
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }
                    ).eq("id", job_id).execute()
                except Exception as exc:
                    logger.error(
                        "Failed to update job to SYNCED",
                        extra={"job_id": job_id, "error": str(exc)},
                    )

                succeeded_product_ids.add(product_id)
                succeeded += 1

            # Mark products that did NOT succeed as FAILED so the recovery worker
            # can reset them to IDLE and the scheduler won't ignore them forever.
            failed_product_ids = (
                {j["product_id"] for j in batch_jobs} - succeeded_product_ids
            )
            for failed_pid in failed_product_ids:
                failed_job_user_id = next(
                    (j["user_id"] for j in batch_jobs if j["product_id"] == failed_pid),
                    None,
                )
                try:
                    db.table("products").update(
                        {
                            "state": "FAILED",
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }
                    ).eq("id", failed_pid).eq("user_id", failed_job_user_id).execute()
                except Exception as exc:
                    logger.error(
                        "Failed to set products.state=FAILED for failed job",
                        extra={
                            "product_id": failed_pid,
                            "user_id": failed_job_user_id,
                            "error": str(exc),
                        },
                    )

            # Record usage event for this batch
            try:
                db.table("usage_events").insert(
                    {
                        "user_id": batch_jobs[0]["user_id"],
                        "event_type": "batch_completed",
                        "platform": None,
                        "product_count": len(batch_jobs),
                        "tokens_input": None,
                        "tokens_output": None,
                        "tokens_cache_read": None,
                        "estimated_cost_usd": None,
                        "metadata": {
                            "batch_id": batch_id,
                            "succeeded": succeeded,
                            "failed": failed,
                        },
                    }
                ).execute()
            except Exception as exc:
                logger.error(
                    "Failed to record usage event",
                    extra={"batch_id": batch_id, "error": str(exc)},
                )

        logger.info(
            "Batch poll cycle complete",
            extra={
                "batches_polled": len(jobs_by_batch),
                "batches_completed": batches_completed,
                "succeeded": succeeded,
                "failed": failed,
            },
        )

        return {
            "succeeded": succeeded,
            "failed": failed,
            "batches_polled": len(jobs_by_batch),
            "batches_completed": batches_completed,
        }
