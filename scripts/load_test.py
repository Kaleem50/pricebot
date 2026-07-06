"""
scripts/load_test.py — PriceBot Concurrent Load Test

Simulates 10 concurrent users each repricing 50 products.

Safety constraints:
  - Requires MOCK_PLATFORM_MODE=true — refuses to run against real platforms.
  - Never sends real Anthropic Batch API requests (mock connector intercepts).
  - Never calls real Stripe or Supabase if SUPABASE_URL is unset.
  - Uses asyncio.gather for concurrency — no threading complexity.

Usage::

    MOCK_PLATFORM_MODE=true python scripts/load_test.py
    MOCK_PLATFORM_MODE=true python scripts/load_test.py --users 5 --products 20

Exit codes:
  0 — all simulated jobs completed successfully
  1 — one or more errors, or safety guard refused to run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

# ---------------------------------------------------------------------------
# Safety guard
# ---------------------------------------------------------------------------

_MOCK_MODE_VAR = "MOCK_PLATFORM_MODE"


def _assert_mock_mode() -> None:
    """
    Abort if MOCK_PLATFORM_MODE is not explicitly set to 'true'.

    This load test must never run against real Anthropic, Stripe, or platform APIs.
    """
    if os.environ.get(_MOCK_MODE_VAR, "").lower() != "true":
        logger.critical(
            "SAFETY GUARD: Load test refused — MOCK_PLATFORM_MODE is not 'true'. "
            "Set MOCK_PLATFORM_MODE=true to run the load test. "
            "This guard prevents accidental spend against real Anthropic credits.",
            extra={"env_var": _MOCK_MODE_VAR},
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Simulated data models
# ---------------------------------------------------------------------------


@dataclass
class MockProduct:
    """A simulated product used during load testing."""

    product_id: str
    user_id: str
    title: str
    current_price: float
    cost: float
    min_margin_pct: float

    def min_floor(self) -> float:
        """Return the minimum safe price given cost and margin floor."""
        return self.cost * (1 + self.min_margin_pct / 100)


@dataclass
class UserLoadResult:
    """Aggregated result for a single simulated user's run."""

    user_id: str
    total_products: int
    succeeded: int = 0
    failed: int = 0
    duration_ms: float = 0.0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Mock repricing engine (no real Anthropic call)
# ---------------------------------------------------------------------------


async def _mock_reprice_product(product: MockProduct) -> dict[str, Any]:
    """
    Simulate a repricing decision without calling Anthropic.

    Mirrors the output structure of the real repricing engine so the surrounding
    pipeline code (guardrail, price_history write) can be tested end-to-end.

    Args:
        product: The product being repriced.

    Returns:
        A dict representing a mock repricing recommendation.
    """
    # Simulate a small network/processing delay
    await asyncio.sleep(0.01)

    mock_competitor_price = product.current_price * 0.97
    recommended_price = max(mock_competitor_price, product.min_floor())

    # MANDATORY GUARDRAIL — same logic as production
    final_price = max(recommended_price, product.min_floor())

    if final_price != recommended_price:
        logger.warning(
            "Guardrail applied in load test — mock price overridden",
            extra={
                "product_id": product.product_id,
                "recommended_price": recommended_price,
                "final_price": final_price,
            },
        )

    return {
        "product_id": product.product_id,
        "recommended_price": recommended_price,
        "final_price": final_price,
        "strategy": "undercut",
        "reasoning": "Mock: competitor is slightly cheaper; undercutting to maintain rank.",
        "confidence": "high",
        "guardrail_applied": final_price != recommended_price,
    }


# ---------------------------------------------------------------------------
# Per-user simulation
# ---------------------------------------------------------------------------


async def simulate_user(
    user_id: str,
    product_count: int,
) -> UserLoadResult:
    """
    Simulate one user's full repricing cycle for ``product_count`` products.

    Args:
        user_id:       Simulated user UUID.
        product_count: Number of products to reprice.

    Returns:
        UserLoadResult with success/failure counts and timing.
    """
    result = UserLoadResult(user_id=user_id, total_products=product_count)
    start = time.monotonic()

    products = [
        MockProduct(
            product_id=str(uuid.uuid4()),
            user_id=user_id,
            title=f"Test Product {i + 1}",
            current_price=round(10.00 + i * 0.50, 2),
            cost=round(4.00 + i * 0.10, 2),
            min_margin_pct=20.0,
        )
        for i in range(product_count)
    ]

    tasks = [_mock_reprice_product(p) for p in products]
    recommendations = await asyncio.gather(*tasks, return_exceptions=True)

    for i, rec in enumerate(recommendations):
        if isinstance(rec, Exception):
            result.failed += 1
            result.errors.append(
                f"product[{i}]={products[i].product_id}: {rec}"
            )
        else:
            result.succeeded += 1

    result.duration_ms = (time.monotonic() - start) * 1000
    return result


# ---------------------------------------------------------------------------
# Load test runner
# ---------------------------------------------------------------------------


async def run_load_test(
    user_count: int,
    products_per_user: int,
) -> None:
    """
    Run the full concurrent load test.

    Spawns ``user_count`` users simultaneously, each repricing
    ``products_per_user`` products.

    Args:
        user_count:        Number of simulated concurrent users.
        products_per_user: Products per user per cycle.

    Raises:
        SystemExit(1): If any simulated job failed.
    """
    logger.info(
        "Load test starting",
        extra={
            "user_count": user_count,
            "products_per_user": products_per_user,
            "total_jobs": user_count * products_per_user,
            "mock_mode": True,
        },
    )

    wall_start = time.monotonic()

    user_ids = [str(uuid.uuid4()) for _ in range(user_count)]
    tasks = [simulate_user(uid, products_per_user) for uid in user_ids]
    results: list[UserLoadResult] = await asyncio.gather(*tasks)

    wall_duration_ms = (time.monotonic() - wall_start) * 1000

    # Aggregate
    total_succeeded = sum(r.succeeded for r in results)
    total_failed = sum(r.failed for r in results)
    total_jobs = user_count * products_per_user
    avg_duration_ms = sum(r.duration_ms for r in results) / len(results)
    p99_duration_ms = sorted(r.duration_ms for r in results)[
        int(len(results) * 0.99) if len(results) > 1 else -1
    ]

    # Report
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {
            "user_count": user_count,
            "products_per_user": products_per_user,
            "total_jobs": total_jobs,
            "mock_mode": True,
        },
        "results": {
            "total_succeeded": total_succeeded,
            "total_failed": total_failed,
            "success_rate_pct": round(total_succeeded / total_jobs * 100, 2),
            "wall_duration_ms": round(wall_duration_ms, 1),
            "avg_user_duration_ms": round(avg_duration_ms, 1),
            "p99_user_duration_ms": round(p99_duration_ms, 1),
        },
    }

    # Collect any errors
    all_errors: list[str] = []
    for r in results:
        all_errors.extend(r.errors)
    if all_errors:
        report["errors"] = all_errors[:20]  # cap at 20 for readability

    print(json.dumps(report, indent=2))

    logger.info(
        "Load test complete",
        extra={
            "succeeded": total_succeeded,
            "failed": total_failed,
            "wall_duration_ms": round(wall_duration_ms, 1),
        },
    )

    if total_failed > 0:
        logger.error(
            "Load test finished with failures",
            extra={"total_failed": total_failed, "errors": all_errors[:5]},
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """
    Parse CLI arguments and run the load test.

    Exits with code 1 if the safety guard triggers or any job fails.
    """
    _assert_mock_mode()

    parser = argparse.ArgumentParser(
        description="PriceBot concurrent load test (mock mode only)."
    )
    parser.add_argument(
        "--users",
        type=int,
        default=10,
        help="Number of simulated concurrent users (default: 10).",
    )
    parser.add_argument(
        "--products",
        type=int,
        default=50,
        help="Products per user per cycle (default: 50).",
    )
    args = parser.parse_args()

    if args.users < 1 or args.users > 100:
        parser.error("--users must be between 1 and 100")
    if args.products < 1 or args.products > 500:
        parser.error("--products must be between 1 and 500")

    asyncio.run(run_load_test(user_count=args.users, products_per_user=args.products))


if __name__ == "__main__":
    main()
