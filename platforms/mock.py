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

from core.repricing_engine import CompetitorProduct, MyProduct
from platforms.base import BasePlatformConnector

logger = logging.getLogger(__name__)

# Test product fixtures (UUIDs match seed_test_products.py for consistency)
_TEST_PRODUCTS = {
    "098abf69-9ad0-5931-a09b-8f2d8d1d5289": MyProduct(
        product_id="098abf69-9ad0-5931-a09b-8f2d8d1d5289",
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
    "f882dfc7-f431-5d5d-857f-ec8f71b71669": MyProduct(
        product_id="f882dfc7-f431-5d5d-857f-ec8f71b71669",
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
    "b69bf742-1304-54e7-9978-260b2dae62bb": MyProduct(
        product_id="b69bf742-1304-54e7-9978-260b2dae62bb",
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
    "8894b55e-4450-56dc-bf82-a890602952c0": MyProduct(
        product_id="8894b55e-4450-56dc-bf82-a890602952c0",
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
    "098abf69-9ad0-5931-a09b-8f2d8d1d5289": [
        CompetitorProduct(price=22.50, platform="amazon", is_fulfilled_by_platform=True, condition="new"),  # type: ignore
        CompetitorProduct(price=23.99, platform="amazon", is_fulfilled_by_platform=False, condition="new"),  # type: ignore
        CompetitorProduct(price=25.50, platform="amazon", is_fulfilled_by_platform=True, condition="new"),  # type: ignore
    ],
    "f882dfc7-f431-5d5d-857f-ec8f71b71669": [
        CompetitorProduct(price=18.00, platform="amazon", is_fulfilled_by_platform=False, condition="new"),  # type: ignore
        CompetitorProduct(price=19.50, platform="amazon", is_fulfilled_by_platform=True, condition="new"),  # type: ignore
    ],
    "b69bf742-1304-54e7-9978-260b2dae62bb": [
        CompetitorProduct(price=52.00, platform="amazon", is_fulfilled_by_platform=True, condition="new"),  # type: ignore
        CompetitorProduct(price=54.99, platform="amazon", is_fulfilled_by_platform=False, condition="new"),  # type: ignore
        CompetitorProduct(price=48.00, platform="amazon", is_fulfilled_by_platform=True, condition="new"),  # type: ignore
        CompetitorProduct(price=51.50, platform="amazon", is_fulfilled_by_platform=False, condition="new"),  # type: ignore
    ],
    "8894b55e-4450-56dc-bf82-a890602952c0": [
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
