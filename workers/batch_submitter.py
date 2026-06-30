"""
workers/batch_submitter.py — Batch Submission Worker

Collects IDLE repricing jobs, fetches competitor prices from platform connectors,
submits batches to the Anthropic Batch API, and records usage events.

Execution model:
  - Called every 15 minutes by the scheduler.
  - Queries the products table for all IDLE products.
  - Respects tier-based limits on product count and daily cycle count.
  - Calls connector.get_competitor_prices_bulk() to fetch live prices.
  - Submits all products for a user in a single Anthropic batch (maximises cache hit).
  - Updates product state to BATCH_SUBMITTED and batch_id.
  - Records a usage_event for cost tracking.

State transition:
  IDLE → BATCH_SUBMITTED (on success)
  IDLE → FAILED (on platform error or tier check failure)

Security constraints (CLAUDE.md §5.4 + SECURITY.md §3):
  - Every DB query filters by user_id.
  - Tier limits are checked BEFORE any Anthropic API call.
  - Platform credentials are decrypted in-memory only, never logged.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from supabase import Client

from api.dependencies import Tier
from core.crypto import decrypt_credential
from core.repricing_engine import MyProduct, RepricingEngine
from platforms import get_connector

logger = logging.getLogger(__name__)

# Tier-based reprice frequency and product count limits (CLAUDE.md §7.4 + task spec)
TIER_LIMITS = {
    Tier.STARTER: {"max_products": 50, "max_daily_cycles": 3},
    Tier.GROWTH: {"max_products": 500, "max_daily_cycles": 6},
    Tier.PRO: {"max_products": 10_000, "max_daily_cycles": 12},
}


class BatchSubmitter:
    """
    Submits a batch of repricing jobs to Anthropic for a single user.

    Typical usage (called by scheduler every 15 min)::

        submitter = BatchSubmitter(anthropic_api_key=settings.ANTHROPIC_API_KEY)
        result = submitter.submit_for_user(user_id="abc-123", db=db_client, tier=tier)

        if result:
            logger.info("Batch submitted", extra=result)
        else:
            logger.info("No products to submit (tier limit or no IDLE products)")
    """

    def __init__(self, anthropic_api_key: str) -> None:
        """
        Initialise the batch submitter with an Anthropic API key.

        Args:
            anthropic_api_key: Anthropic API key from ANTHROPIC_API_KEY env var.
                              Never logged or exposed.
        """
        if not anthropic_api_key:
            raise ValueError("anthropic_api_key must not be empty")
        self._engine = RepricingEngine(api_key=anthropic_api_key)

    async def submit_for_user(
        self, user_id: str, db: Client, tier: Tier
    ) -> dict[str, Any] | None:
        """
        Collect and submit IDLE products for a user to the Anthropic Batch API.

        Flow:
          1. Query repricing_jobs WHERE user_id AND state='IDLE'.
          2. Check tier limits (product count and daily cycle count).
          3. For each product: fetch competitor prices via connector.
          4. Call engine.submit_batch() with (product, competitors) tuples.
          5. Update repricing_jobs state to BATCH_SUBMITTED, set batch_id.
          6. Record a usage_event type='batch_submitted'.

        Args:
            user_id: Supabase user ID — all queries filtered by this.
            db:      Supabase client.
            tier:    User's subscription tier (from subscriptions table).

        Returns:
            Dict with keys:
              - success: bool
              - batch_id: str (if success=True)
              - product_count: int (number of products in batch)
              - estimated_tokens: int (rough estimate for cost tracking)
            Or None if no products to submit or tier check fails.

        Raises:
            Exception: On unexpected DB errors (not caught — logged as ERROR
                      and allowed to bubble up to scheduler for logging).
        """
        # Step 1: Query IDLE products for this user
        logger.info(
            "Batch submitter: querying IDLE products",
            extra={"user_id": user_id, "tier": tier.name},
        )

        try:
            idle_result = (
                db.table("products")
                .select(
                    "id, platform, platform_product_id, platform_sku, title, current_price, cost, min_margin_floor, last_repriced_at"
                )
                .eq("user_id", user_id)
                .eq("state", "IDLE")
                .eq("is_tracking", True)
                .order("last_repriced_at", desc=False)
                .execute()
            )
        except Exception as exc:
            logger.error(
                "Failed to query IDLE products",
                extra={"user_id": user_id, "error": str(exc)},
            )
            raise

        idle_products = idle_result.data or []
        if not idle_products:
            logger.info(
                "No IDLE products to submit",
                extra={"user_id": user_id},
            )
            return None

        # Step 2: Check tier limits
        product_limit = TIER_LIMITS[tier]["max_products"]
        daily_cycle_limit = TIER_LIMITS[tier]["max_daily_cycles"]

        if len(idle_products) > product_limit:
            logger.warning(
                "Product count exceeds tier limit — truncating",
                extra={
                    "user_id": user_id,
                    "tier": tier.name,
                    "idle_count": len(idle_products),
                    "tier_limit": product_limit,
                },
            )
            idle_products = idle_products[:product_limit]

        # Check daily cycle count
        try:
            cycle_result = (
                db.table("usage_events")
                .select("id")
                .eq("user_id", user_id)
                .eq("event_type", "batch_submitted")
                .gte(
                    "created_at",
                    datetime.now(timezone.utc).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    ).isoformat(),
                )
                .execute()
            )
        except Exception as exc:
            logger.error(
                "Failed to query cycle count from usage_events",
                extra={"user_id": user_id, "error": str(exc)},
            )
            raise

        cycle_count = len(cycle_result.data or [])
        if cycle_count >= daily_cycle_limit:
            logger.warning(
                "Daily cycle limit reached — skipping submission",
                extra={
                    "user_id": user_id,
                    "tier": tier.name,
                    "cycle_count": cycle_count,
                    "daily_limit": daily_cycle_limit,
                },
            )
            return None

        # Step 3: Fetch competitor prices for products
        logger.info(
            "Fetching competitor prices",
            extra={"user_id": user_id, "product_count": len(idle_products)},
        )

        # Build products_by_id from idle_products data
        products_by_id = {p["id"]: p for p in idle_products}
        if not products_by_id:
            logger.info("No products found for submission", extra={"user_id": user_id})
            return None

        # Group products by platform so we can instantiate connectors
        products_by_platform: dict[str, list[dict]] = {}
        for product in products_by_id.values():
            platform = product["platform"]
            if platform not in products_by_platform:
                products_by_platform[platform] = []
            products_by_platform[platform].append(product)

        # Fetch platform credentials and competitor prices per platform
        products_with_competitors: list[tuple[MyProduct, list[Any]]] = []

        for platform, products_on_platform in products_by_platform.items():
            logger.info(
                "Fetching credentials and competitor prices for platform",
                extra={
                    "user_id": user_id,
                    "platform": platform,
                    "product_count": len(products_on_platform),
                },
            )

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
                    "Failed to fetch platform credentials",
                    extra={
                        "user_id": user_id,
                        "platform": platform,
                        "error": str(exc),
                    },
                )
                raise

            if not cred_result.data:
                logger.warning(
                    "No active platform connection found — skipping platform",
                    extra={"user_id": user_id, "platform": platform},
                )
                continue

            encrypted_creds = cred_result.data[0]["encrypted_creds"]
            try:
                creds_json = decrypt_credential(encrypted_creds)
                creds_dict = json.loads(creds_json)
            except Exception as exc:
                logger.critical(
                    "Failed to decrypt platform credentials",
                    extra={
                        "user_id": user_id,
                        "platform": platform,
                        "error": str(exc),
                    },
                )
                continue

            # Instantiate connector for this platform
            try:
                connector = get_connector(
                    platform=platform,
                    credentials=creds_dict,
                    user_id=user_id,
                    db=db,
                )
            except Exception as exc:
                logger.error(
                    "Failed to instantiate connector",
                    extra={
                        "user_id": user_id,
                        "platform": platform,
                        "error": str(exc),
                    },
                )
                continue

            # Convert DB rows to MyProduct instances
            my_products = [
                MyProduct(
                    product_id=p["id"],
                    platform_product_id=p["platform_product_id"],
                    platform_sku=p.get("platform_sku"),
                    title=p["title"],
                    platform=p["platform"],
                    current_price=float(p["current_price"]),
                    cost=float(p.get("cost") or 0),
                    min_margin_floor=float(p.get("min_margin_floor") or 0),
                    user_id=user_id,
                    platform_context=p.get("platform_context") or {},
                    metadata=p.get("metadata") or {},
                )
                for p in products_on_platform
            ]

            # Fetch competitor prices (async)
            try:
                competitors_bulk = await connector.get_competitor_prices_bulk(my_products)
            except Exception as exc:
                logger.error(
                    "Failed to fetch competitor prices",
                    extra={
                        "user_id": user_id,
                        "platform": platform,
                        "error": str(exc),
                    },
                )
                continue

            # Build (product, competitors) tuples
            for product in my_products:
                competitors = competitors_bulk.get(product.product_id, [])
                products_with_competitors.append((product, competitors))

        if not products_with_competitors:
            logger.info(
                "No products with competitors — nothing to submit",
                extra={"user_id": user_id},
            )
            return None

        # Step 4: Submit batch to Anthropic
        logger.info(
            "Submitting batch to Anthropic",
            extra={
                "user_id": user_id,
                "product_count": len(products_with_competitors),
            },
        )

        try:
            batch_result = self._engine.submit_batch(
                user_id=user_id,
                products_with_competitors=products_with_competitors,
            )
        except Exception as exc:
            logger.critical(
                "Anthropic batch submission failed",
                extra={
                    "user_id": user_id,
                    "product_count": len(products_with_competitors),
                    "error": str(exc),
                },
            )
            raise

        # Step 5: INSERT a repricing_job row for each product and flip products.state.
        # repricing_jobs rows do NOT exist before this point — the submitter creates them.
        # Per-product loop is required so each row gets its own anthropic_custom_id token.
        logger.info(
            "Creating repricing_jobs rows and updating product states to BATCH_SUBMITTED",
            extra={"user_id": user_id, "batch_id": batch_result.batch_id},
        )

        # Invert custom_id_map (custom_id → product_id) to product_id → custom_id
        # so we can look up the right token for each product in one pass.
        product_id_to_custom_id = {v: k for k, v in batch_result.custom_id_map.items()}
        now_iso = datetime.now(timezone.utc).isoformat()

        for product, _competitors in products_with_competitors:
            custom_id = product_id_to_custom_id.get(product.product_id)

            # INSERT new repricing_job row (the row does not exist yet).
            try:
                db.table("repricing_jobs").insert(
                    {
                        "user_id": user_id,
                        "product_id": product.product_id,
                        "platform": product.platform,
                        "state": "BATCH_SUBMITTED",
                        "batch_id": batch_result.batch_id,
                        "anthropic_custom_id": custom_id,
                        "submitted_at": now_iso,
                        "scheduled_at": now_iso,
                        "created_at": now_iso,
                        "updated_at": now_iso,
                    }
                ).execute()
            except Exception as exc:
                logger.error(
                    "Failed to insert repricing_job for product",
                    extra={
                        "user_id": user_id,
                        "product_id": product.product_id,
                        "batch_id": batch_result.batch_id,
                        "error": str(exc),
                    },
                )
                raise

            # UPDATE products.state so the scheduler won't re-pick this product
            # before the batch result arrives.
            try:
                db.table("products").update(
                    {
                        "state": "BATCH_SUBMITTED",
                        "updated_at": now_iso,
                    }
                ).eq("id", product.product_id).eq("user_id", user_id).execute()
            except Exception as exc:
                logger.error(
                    "Failed to update products.state to BATCH_SUBMITTED",
                    extra={
                        "user_id": user_id,
                        "product_id": product.product_id,
                        "error": str(exc),
                    },
                )
                raise

        # Step 6: Record usage event
        logger.info(
            "Recording usage event",
            extra={"user_id": user_id, "event_type": "batch_submitted"},
        )

        try:
            db.table("usage_events").insert(
                {
                    "user_id": user_id,
                    "event_type": "batch_submitted",
                    "platform": None,
                    "product_count": len(products_with_competitors),
                    "tokens_input": batch_result.estimated_input_tokens,
                    "tokens_output": None,
                    "tokens_cache_read": None,
                    "estimated_cost_usd": None,
                    "metadata": {
                        "batch_id": batch_result.batch_id,
                        "submitted_at": batch_result.submitted_at.isoformat(),
                    },
                }
            ).execute()
        except Exception as exc:
            logger.error(
                "Failed to record usage event",
                extra={"user_id": user_id, "error": str(exc)},
            )

        return {
            "success": True,
            "batch_id": batch_result.batch_id,
            "product_count": len(products_with_competitors),
            "estimated_tokens": batch_result.estimated_input_tokens,
        }
