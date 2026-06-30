"""
platforms/mock.py — Mock Platform Connector for Testing

Implements BasePlatformConnector with hardcoded test data for end-to-end
testing of the worker pipeline without real platform credentials.

Test Products:
  - Product A: Normal case (guardrail not triggered)
  - Product B: Guardrail trigger (floor exceeds current price)
  - Product C: Premium strategy case
  - Product D: Error handling case (malformed response)

All methods return immediately without I/O or real API calls.
"""

from __future__ import annotations

import logging
from typing import Any

from core.repricing_engine import CompetitorProduct, MyProduct
from platforms.base import BasePlatformConnector

logger = logging.getLogger(__name__)

# Test product fixtures
_TEST_PRODUCTS = {
    "prod-a": MyProduct(
        product_id="prod-a",
        platform_product_id="ASIN-A001",
        platform_sku="SKU-A001",
        title="Test Product A - Normal Case",
        platform="amazon",  # type: ignore
        current_price=24.99,
        cost=12.00,
        min_margin_floor=3.60,
        user_id="mock-user",
        platform_context={},
        metadata={},
    ),
    "prod-b": MyProduct(
        product_id="prod-b",
        platform_product_id="ASIN-B001",
        platform_sku="SKU-B001",
        title="Test Product B - Guardrail Trigger",
        platform="amazon",  # type: ignore
        current_price=19.99,
        cost=15.00,
        min_margin_floor=8.00,
        user_id="mock-user",
        platform_context={},
        metadata={},
    ),
    "prod-c": MyProduct(
        product_id="prod-c",
        platform_product_id="ASIN-C001",
        platform_sku="SKU-C001",
        title="Test Product C - Premium Case",
        platform="amazon",  # type: ignore
        current_price=49.99,
        cost=20.00,
        min_margin_floor=5.00,
        user_id="mock-user",
        platform_context={},
        metadata={},
    ),
    "prod-d": MyProduct(
        product_id="prod-d",
        platform_product_id="ASIN-D001",
        platform_sku="SKU-D001",
        title="Test Product D - Error Handling",
        platform="amazon",  # type: ignore
        current_price=15.00,
        cost=8.00,
        min_margin_floor=2.00,
        user_id="mock-user",
        platform_context={},
        metadata={},
    ),
}

# Competitor prices per product
_TEST_COMPETITORS = {
    "prod-a": [
        CompetitorProduct(price=22.50, platform="amazon", is_fulfilled_by_platform=True, condition="new"),  # type: ignore
        CompetitorProduct(price=23.99, platform="amazon", is_fulfilled_by_platform=False, condition="new"),  # type: ignore
        CompetitorProduct(price=25.50, platform="amazon", is_fulfilled_by_platform=True, condition="new"),  # type: ignore
    ],
    "prod-b": [
        CompetitorProduct(price=18.00, platform="amazon", is_fulfilled_by_platform=False, condition="new"),  # type: ignore
        CompetitorProduct(price=19.50, platform="amazon", is_fulfilled_by_platform=True, condition="new"),  # type: ignore
    ],
    "prod-c": [
        CompetitorProduct(price=52.00, platform="amazon", is_fulfilled_by_platform=True, condition="new"),  # type: ignore
        CompetitorProduct(price=54.99, platform="amazon", is_fulfilled_by_platform=False, condition="new"),  # type: ignore
        CompetitorProduct(price=48.00, platform="amazon", is_fulfilled_by_platform=True, condition="new"),  # type: ignore
        CompetitorProduct(price=51.50, platform="amazon", is_fulfilled_by_platform=False, condition="new"),  # type: ignore
    ],
    "prod-d": [
        CompetitorProduct(price=14.50, platform="amazon", is_fulfilled_by_platform=True, condition="new"),  # type: ignore
    ],
}


class MockConnector(BasePlatformConnector):
    """
    Mock platform connector for testing the worker pipeline.

    Returns hardcoded test data for 4 products (A, B, C, D) covering:
      - Normal repricing case
      - Guardrail trigger case (floor > current price)
      - Premium strategy case
      - Error handling case

    No real API calls are made. All methods return immediately.
    """

    async def validate_credentials(self) -> bool:
        """Always return True — mock credentials are always valid."""
        logger.info("Mock connector: credentials validated", extra={"user_id": self.user_id})
        return True

    async def get_products(self) -> list[MyProduct]:
        """Return all 4 test products with user_id stamped."""
        products = [prod for prod in _TEST_PRODUCTS.values()]
        for prod in products:
            prod.user_id = self.user_id
        logger.info(
            "Mock connector: returning test products",
            extra={"user_id": self.user_id, "product_count": len(products)},
        )
        return products

    async def _get_competitor_prices_impl(self, product: MyProduct) -> list[CompetitorProduct]:
        """Return hardcoded competitors for the test product."""
        competitors = _TEST_COMPETITORS.get(product.product_id, [])
        logger.debug(
            "Mock connector: competitor prices fetched",
            extra={
                "product_id": product.product_id,
                "competitor_count": len(competitors),
            },
        )
        return competitors

    async def _apply_price_impl(self, product: MyProduct, new_price: float) -> None:
        """Log the price application and return immediately."""
        delta = new_price - product.current_price
        logger.info(
            "Mock connector: price applied",
            extra={
                "product_id": product.product_id,
                "old_price": product.current_price,
                "new_price": new_price,
                "delta": round(delta, 2),
            },
        )
