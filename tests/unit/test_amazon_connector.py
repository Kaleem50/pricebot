"""
tests/unit/test_amazon_connector.py — Amazon SP-API Connector Tests

Tests for platforms/amazon.py covering:
  - validate_credentials: valid and invalid tokens
  - get_products: pagination across two pages
  - get_competitor_prices: single-ASIN batch and rate-limit handling
  - apply_price: success, 429 retry, 404 not-found
  - _get_access_token: LWA token caching (only one LWA call for two requests)

All HTTP calls are mocked with unittest.mock to avoid hitting real APIs.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.repricing_engine import MyProduct
from platforms.amazon import AmazonConnector
from platforms.exceptions import (
    PlatformAuthError,
    PlatformProductNotFoundError,
    PlatformRateLimitError,
)
from tests.fixtures.amazon_responses import (
    COMPETITIVE_PRICING_SUCCESS,
    LISTINGS_PAGE_1,
    LISTINGS_PAGE_2,
    LISTINGS_PATCH_SUCCESS,
    LISTINGS_SINGLE,
    LWA_TOKEN_SUCCESS,
    make_credentials,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_ID = "user-uuid-test-001"


def _make_connector(credentials: dict | None = None) -> AmazonConnector:
    """Create an AmazonConnector with test credentials."""
    return AmazonConnector(
        credentials=credentials or make_credentials(),
        user_id=_USER_ID,
    )


def _mock_response(status_code: int = 200, json_data: dict | None = None) -> MagicMock:
    """Build a mock httpx.Response object."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.is_success = 200 <= status_code < 300
    resp.json = MagicMock(return_value=json_data or {})
    resp.text = str(json_data or {})
    resp.headers = {}
    return resp


def _sample_product(
    asin: str = "B001TEST01",
    sku: str = "SKU-001",
    price: float = 24.99,
) -> MyProduct:
    """Build a minimal MyProduct for testing."""
    return MyProduct(
        product_id="product-uuid-001",
        platform_product_id=asin,
        platform_sku=sku,
        title="Widget Pro 500ml",
        platform="amazon",
        current_price=price,
        cost=10.0,
        min_margin_floor=2.0,
        user_id=_USER_ID,
    )


# ---------------------------------------------------------------------------
# Fixture: clear token cache before each test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_token_cache():
    """Reset the class-level LWA token cache before every test."""
    AmazonConnector._token_cache.clear()
    yield
    AmazonConnector._token_cache.clear()


# ---------------------------------------------------------------------------
# Tests: validate_credentials
# ---------------------------------------------------------------------------


class TestValidateCredentials:
    """validate_credentials should return True for valid creds, False for 403."""

    @pytest.mark.asyncio
    async def test_valid_credentials_returns_true(self):
        connector = _make_connector()

        # Mock LWA token exchange
        lwa_resp = _mock_response(200, LWA_TOKEN_SUCCESS)
        # Mock SP-API listings call (any 200 = valid)
        listings_resp = _mock_response(200, LISTINGS_SINGLE)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=lwa_resp)
        mock_client.get = AsyncMock(return_value=listings_resp)

        with patch("platforms.amazon.httpx.AsyncClient", return_value=mock_client):
            result = await connector.validate_credentials()

        assert result is True

    @pytest.mark.asyncio
    async def test_invalid_token_returns_false(self):
        connector = _make_connector()

        lwa_resp = _mock_response(200, LWA_TOKEN_SUCCESS)
        forbidden_resp = _mock_response(403, {"errors": [{"message": "Forbidden"}]})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=lwa_resp)
        mock_client.get = AsyncMock(return_value=forbidden_resp)

        with patch("platforms.amazon.httpx.AsyncClient", return_value=mock_client):
            result = await connector.validate_credentials()

        assert result is False

    @pytest.mark.asyncio
    async def test_lwa_failure_returns_false(self):
        connector = _make_connector()

        lwa_fail = _mock_response(400, {"error": "invalid_grant"})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=lwa_fail)

        with patch("platforms.amazon.httpx.AsyncClient", return_value=mock_client):
            result = await connector.validate_credentials()

        assert result is False

    @pytest.mark.asyncio
    async def test_server_error_raises_platform_api_error(self):
        connector = _make_connector()

        lwa_resp = _mock_response(200, LWA_TOKEN_SUCCESS)
        server_err = _mock_response(503, {"errors": [{"message": "Service unavailable"}]})
        server_err.is_success = False

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=lwa_resp)
        mock_client.get = AsyncMock(return_value=server_err)

        from platforms.exceptions import PlatformAPIError
        with patch("platforms.amazon.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(PlatformAPIError):
                await connector.validate_credentials()


# ---------------------------------------------------------------------------
# Tests: get_products (pagination)
# ---------------------------------------------------------------------------


class TestGetProducts:
    """get_products fetches all pages and returns combined product list."""

    @pytest.mark.asyncio
    async def test_fetches_two_pages(self):
        connector = _make_connector()

        lwa_resp = _mock_response(200, LWA_TOKEN_SUCCESS)
        page1 = _mock_response(200, LISTINGS_PAGE_1)
        page2 = _mock_response(200, LISTINGS_PAGE_2)

        # post → LWA; get calls: page1 then page2
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=lwa_resp)
        mock_client.get = AsyncMock(side_effect=[page1, page2])

        with patch("platforms.amazon.httpx.AsyncClient", return_value=mock_client):
            products = await connector.get_products()

        assert len(products) == 2
        assert products[0].platform_product_id == "B001TEST01"
        assert products[0].current_price == 24.99
        assert products[1].platform_product_id == "B002TEST02"
        assert products[1].current_price == 14.99

    @pytest.mark.asyncio
    async def test_empty_catalog_returns_empty_list(self):
        connector = _make_connector()

        lwa_resp = _mock_response(200, LWA_TOKEN_SUCCESS)
        empty = _mock_response(200, {"numberOfResults": 0, "items": []})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=lwa_resp)
        mock_client.get = AsyncMock(return_value=empty)

        with patch("platforms.amazon.httpx.AsyncClient", return_value=mock_client):
            products = await connector.get_products()

        assert products == []

    @pytest.mark.asyncio
    async def test_products_stamped_with_user_id(self):
        connector = _make_connector()

        lwa_resp = _mock_response(200, LWA_TOKEN_SUCCESS)
        page = _mock_response(200, LISTINGS_SINGLE)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=lwa_resp)
        mock_client.get = AsyncMock(return_value=page)

        with patch("platforms.amazon.httpx.AsyncClient", return_value=mock_client):
            products = await connector.get_products()

        assert all(p.user_id == _USER_ID for p in products)
        assert all(p.platform == "amazon" for p in products)


# ---------------------------------------------------------------------------
# Tests: get_competitor_prices (batching + 429 retry)
# ---------------------------------------------------------------------------


class TestGetCompetitorPrices:
    """get_competitor_prices returns parsed CompetitorProduct list."""

    @pytest.mark.asyncio
    async def test_returns_competitor_list(self):
        connector = _make_connector()
        product = _sample_product()

        lwa_resp = _mock_response(200, LWA_TOKEN_SUCCESS)
        pricing_resp = _mock_response(200, COMPETITIVE_PRICING_SUCCESS)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=lwa_resp)
        mock_client.get = AsyncMock(return_value=pricing_resp)

        with patch("platforms.amazon.httpx.AsyncClient", return_value=mock_client):
            with patch("platforms.amazon.asyncio.sleep", new_callable=AsyncMock):
                competitors = await connector._get_competitor_prices_impl(product)

        assert len(competitors) == 2
        prices = {c.price for c in competitors}
        assert 22.99 in prices
        assert 23.49 in prices
        assert all(c.platform == "amazon" for c in competitors)

    @pytest.mark.asyncio
    async def test_empty_competitors_returns_empty_list(self):
        from tests.fixtures.amazon_responses import COMPETITIVE_PRICING_NO_COMPETITORS
        connector = _make_connector()
        product = _sample_product()

        lwa_resp = _mock_response(200, LWA_TOKEN_SUCCESS)
        pricing_resp = _mock_response(200, COMPETITIVE_PRICING_NO_COMPETITORS)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=lwa_resp)
        mock_client.get = AsyncMock(return_value=pricing_resp)

        with patch("platforms.amazon.httpx.AsyncClient", return_value=mock_client):
            with patch("platforms.amazon.asyncio.sleep", new_callable=AsyncMock):
                competitors = await connector._get_competitor_prices_impl(product)

        assert competitors == []

    @pytest.mark.asyncio
    async def test_429_raises_platform_rate_limit_error(self):
        connector = _make_connector()
        product = _sample_product()

        lwa_resp = _mock_response(200, LWA_TOKEN_SUCCESS)
        rate_limited = _mock_response(429, {})
        rate_limited.headers = {"retry-after": "2"}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=lwa_resp)
        mock_client.get = AsyncMock(return_value=rate_limited)

        with patch("platforms.amazon.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(PlatformRateLimitError):
                await connector._get_competitor_prices_impl(product)


# ---------------------------------------------------------------------------
# Tests: apply_price (success + 429 retry via base class)
# ---------------------------------------------------------------------------


class TestApplyPrice:
    """apply_price updates listing and returns True on success."""

    @pytest.mark.asyncio
    async def test_apply_price_success(self):
        connector = _make_connector()
        product = _sample_product()

        lwa_resp = _mock_response(200, LWA_TOKEN_SUCCESS)
        patch_resp = _mock_response(202, LISTINGS_PATCH_SUCCESS)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=lwa_resp)
        mock_client.patch = AsyncMock(return_value=patch_resp)

        with patch("platforms.amazon.httpx.AsyncClient", return_value=mock_client):
            result = await connector._apply_price_impl(product, 22.49)

        assert result is True
        # Verify the PATCH was called with the correct price
        call_kwargs = mock_client.patch.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json") or call_kwargs[0][1]
        assert body["patches"][0]["value"][0]["our_price"][0]["schedule"][0]["value_with_tax"] == 22.49

    @pytest.mark.asyncio
    async def test_apply_price_429_raises_rate_limit_error(self):
        connector = _make_connector()
        product = _sample_product()

        lwa_resp = _mock_response(200, LWA_TOKEN_SUCCESS)
        too_many = _mock_response(429, {})
        too_many.headers = {"retry-after": "1"}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=lwa_resp)
        mock_client.patch = AsyncMock(return_value=too_many)

        with patch("platforms.amazon.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(PlatformRateLimitError):
                await connector._apply_price_impl(product, 22.49)

    @pytest.mark.asyncio
    async def test_apply_price_404_raises_not_found(self):
        connector = _make_connector()
        product = _sample_product()

        lwa_resp = _mock_response(200, LWA_TOKEN_SUCCESS)
        not_found = _mock_response(404, {"errors": [{"message": "SKU not found"}]})
        not_found.is_success = False

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=lwa_resp)
        mock_client.patch = AsyncMock(return_value=not_found)

        with patch("platforms.amazon.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(PlatformProductNotFoundError):
                await connector._apply_price_impl(product, 22.49)

    @pytest.mark.asyncio
    async def test_apply_price_requires_sku(self):
        connector = _make_connector()
        product = _sample_product(sku="")
        # Build a product with no SKU
        no_sku_product = MyProduct(
            product_id="prod-001",
            platform_product_id="B001TEST01",
            platform_sku=None,
            title="Test",
            platform="amazon",
            current_price=20.0,
            user_id=_USER_ID,
        )

        lwa_resp = _mock_response(200, LWA_TOKEN_SUCCESS)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=lwa_resp)

        from platforms.exceptions import PlatformAPIError
        with patch("platforms.amazon.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(PlatformAPIError, match="platform_sku is None"):
                await connector._apply_price_impl(no_sku_product, 22.49)


# ---------------------------------------------------------------------------
# Tests: LWA token caching
# ---------------------------------------------------------------------------


class TestTokenCaching:
    """Token is cached per user_id and reused across calls within expiry window."""

    @pytest.mark.asyncio
    async def test_token_cached_across_calls(self):
        connector = _make_connector()

        lwa_resp = _mock_response(200, LWA_TOKEN_SUCCESS)
        listing_resp = _mock_response(200, LISTINGS_SINGLE)

        lwa_call_count = 0

        async def count_post(*args, **kwargs):
            nonlocal lwa_call_count
            lwa_call_count += 1
            return lwa_resp

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=count_post)
        mock_client.get = AsyncMock(return_value=listing_resp)

        with patch("platforms.amazon.httpx.AsyncClient", return_value=mock_client):
            await connector.validate_credentials()
            await connector.validate_credentials()

        # Only one LWA token exchange despite two calls
        assert lwa_call_count == 1, (
            f"Expected 1 LWA call, got {lwa_call_count} — token caching is not working"
        )

    @pytest.mark.asyncio
    async def test_expired_token_triggers_refresh(self):
        connector = _make_connector()

        # Pre-populate cache with an already-expired token
        AmazonConnector._token_cache[_USER_ID] = ("old-token", time.time() - 10)

        lwa_resp = _mock_response(200, LWA_TOKEN_SUCCESS)
        listing_resp = _mock_response(200, LISTINGS_SINGLE)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=lwa_resp)
        mock_client.get = AsyncMock(return_value=listing_resp)

        with patch("platforms.amazon.httpx.AsyncClient", return_value=mock_client):
            await connector.validate_credentials()

        # Token should be refreshed
        assert mock_client.post.called
        new_token, _ = AmazonConnector._token_cache[_USER_ID]
        assert new_token == LWA_TOKEN_SUCCESS["access_token"]

    @pytest.mark.asyncio
    async def test_different_users_have_separate_tokens(self):
        conn_a = AmazonConnector(credentials=make_credentials(), user_id="user-a")
        conn_b = AmazonConnector(credentials=make_credentials(), user_id="user-b")

        lwa_a = _mock_response(200, {**LWA_TOKEN_SUCCESS, "access_token": "token-for-a"})
        lwa_b = _mock_response(200, {**LWA_TOKEN_SUCCESS, "access_token": "token-for-b"})
        listing_resp = _mock_response(200, LISTINGS_SINGLE)

        post_responses = [lwa_a, lwa_b]
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=post_responses)
        mock_client.get = AsyncMock(return_value=listing_resp)

        with patch("platforms.amazon.httpx.AsyncClient", return_value=mock_client):
            await conn_a.validate_credentials()
            await conn_b.validate_credentials()

        token_a = AmazonConnector._token_cache.get("user-a", (None,))[0]
        token_b = AmazonConnector._token_cache.get("user-b", (None,))[0]
        assert token_a == "token-for-a"
        assert token_b == "token-for-b"
        assert token_a != token_b
