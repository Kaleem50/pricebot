"""
tests/unit/test_etsy_connector.py — Etsy API Connector Tests

Tests for platforms/etsy.py covering:
  - validate_credentials: success (200) and failure (401)
  - get_products: single page, two-page pagination
  - get_products: price conversion (amount=2499, divisor=100 → 24.99)
  - get_competitor_prices: keyword search, filters own shop correctly
  - get_competitor_prices: daily rate limit raises PlatformRateLimitError
  - apply_price: success (200 and 204) and API error (400)
  - Auto-refresh: 401 triggers token refresh then retries
  - Second 401 after refresh raises PlatformAuthError

All HTTP calls are mocked with unittest.mock to avoid hitting real APIs.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.repricing_engine import MyProduct
from platforms.etsy import EtsyConnector
from platforms.exceptions import (
    PlatformAPIError,
    PlatformAuthError,
    PlatformRateLimitError,
)
from tests.fixtures.etsy_responses import (
    COMPETITOR_SEARCH_RESULTS,
    LISTINGS_PAGE_1,
    LISTINGS_PAGE_2,
    LISTINGS_SINGLE_PAGE,
    PATCH_SUCCESS,
    TOKEN_REFRESH_SUCCESS,
    USERS_ME_SUCCESS,
    make_credentials,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_ID = "user-uuid-etsy-test-001"
_SHOP_ID = "99999999"


def _make_connector(credentials: dict | None = None, db: object | None = None) -> EtsyConnector:
    """Create an EtsyConnector with test credentials."""
    return EtsyConnector(
        credentials=credentials or make_credentials(),
        user_id=_USER_ID,
        db=db,
    )


def _mock_response(
    status_code: int = 200,
    json_data: dict | None = None,
    is_success: bool | None = None,
) -> MagicMock:
    """Build a mock httpx.Response object."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.is_success = (200 <= status_code < 300) if is_success is None else is_success
    resp.json = MagicMock(return_value=json_data or {})
    resp.text = str(json_data or {})
    return resp


def _sample_product(
    listing_id: str = "100000001",
    price: float = 28.00,
    title: str = "Handmade Ceramic Coffee Mug",
) -> MyProduct:
    """Build a minimal MyProduct for Etsy testing."""
    return MyProduct(
        product_id=listing_id,
        platform_product_id=listing_id,
        platform_sku=None,
        title=title,
        platform="etsy",
        current_price=price,
        cost=8.0,
        min_margin_floor=2.0,
        user_id=_USER_ID,
    )


# ---------------------------------------------------------------------------
# EtsyConnector.__init__
# ---------------------------------------------------------------------------


class TestInit:
    """Initialisation and credential validation."""

    def test_missing_access_token_raises_value_error(self):
        """Connector must reject credentials missing access_token."""
        with pytest.raises(ValueError, match="access_token"):
            EtsyConnector(
                credentials={"refresh_token": "rt", "shop_id": "1"},
                user_id=_USER_ID,
            )

    def test_missing_refresh_token_raises_value_error(self):
        """Connector must reject credentials missing refresh_token."""
        with pytest.raises(ValueError, match="refresh_token"):
            EtsyConnector(
                credentials={"access_token": "at", "shop_id": "1"},
                user_id=_USER_ID,
            )

    def test_missing_shop_id_raises_value_error(self):
        """Connector must reject credentials missing shop_id."""
        with pytest.raises(ValueError, match="shop_id"):
            EtsyConnector(
                credentials={"access_token": "at", "refresh_token": "rt"},
                user_id=_USER_ID,
            )

    def test_valid_credentials_accepted(self):
        """Connector initialises without error given all required keys."""
        connector = _make_connector()
        assert connector.user_id == _USER_ID
        assert connector._credentials["shop_id"] == _SHOP_ID


# ---------------------------------------------------------------------------
# validate_credentials
# ---------------------------------------------------------------------------


class TestValidateCredentials:
    """Tests for validate_credentials()."""

    def test_success_returns_true(self):
        """200 response from /users/me returns True."""
        connector = _make_connector()
        mock_resp = _mock_response(200, USERS_ME_SUCCESS)

        with patch.object(connector, "_request", new=AsyncMock(return_value=mock_resp)):
            result = asyncio.run(connector.validate_credentials())

        assert result is True

    def test_auth_error_on_401(self):
        """401 during validate_credentials raises PlatformAuthError."""
        connector = _make_connector()

        with patch.object(
            connector,
            "_request",
            new=AsyncMock(side_effect=PlatformAuthError("401", platform="etsy", status_code=401)),
        ):
            with pytest.raises(PlatformAuthError):
                asyncio.run(connector.validate_credentials())

    def test_auth_error_on_403(self):
        """403 during validate_credentials raises PlatformAuthError."""
        connector = _make_connector()

        with patch.object(
            connector,
            "_request",
            new=AsyncMock(side_effect=PlatformAuthError("403", platform="etsy", status_code=403)),
        ):
            with pytest.raises(PlatformAuthError):
                asyncio.run(connector.validate_credentials())

    def test_non_2xx_raises_platform_api_error(self):
        """Unexpected 500 from /users/me raises PlatformAPIError."""
        connector = _make_connector()
        mock_resp = _mock_response(500, {}, is_success=False)

        with patch.object(connector, "_request", new=AsyncMock(return_value=mock_resp)):
            with pytest.raises(PlatformAPIError):
                asyncio.run(connector.validate_credentials())


# ---------------------------------------------------------------------------
# get_products — price conversion
# ---------------------------------------------------------------------------


class TestGetProductsPriceConversion:
    """Price conversion: Etsy returns amount/divisor integers."""

    def test_price_conversion_amount_divisor(self):
        """amount=2499, divisor=100 must yield current_price=24.99."""
        connector = _make_connector()

        single_listing_response = {
            "count": 1,
            "results": [
                {
                    "listing_id": 999888777,
                    "state": "active",
                    "title": "Precision Price Test Item",
                    "price": {"amount": 2499, "divisor": 100, "currency_code": "USD"},
                    "url": "https://www.etsy.com/listing/999888777/test",
                }
            ],
        }
        # Return the listing on first call, then empty results to end pagination
        mock_first = _mock_response(200, single_listing_response)
        mock_empty = _mock_response(200, {"count": 0, "results": []})

        with patch.object(
            connector, "_request", new=AsyncMock(side_effect=[mock_first, mock_empty])
        ):
            products = asyncio.run(connector.get_products())

        assert len(products) == 1
        assert products[0].current_price == 24.99

    def test_price_conversion_zero_divisor_filtered(self):
        """divisor=0 must not raise ZeroDivisionError — listing is skipped (price=0.0)."""
        connector = _make_connector()

        bad_listing_response = {
            "count": 1,
            "results": [
                {
                    "listing_id": 111222333,
                    "state": "active",
                    "title": "Bad Price Item",
                    "price": {"amount": 1000, "divisor": 0, "currency_code": "USD"},
                    "url": "https://www.etsy.com/listing/111222333/bad",
                }
            ],
        }
        mock_first = _mock_response(200, bad_listing_response)

        with patch.object(
            connector, "_request", new=AsyncMock(return_value=mock_first)
        ):
            # Must not raise — zero-price listing is silently skipped
            products = asyncio.run(connector.get_products())

        # Zero-price listing is filtered out to avoid Pydantic gt=0 rejection
        assert products == []


# ---------------------------------------------------------------------------
# get_products — pagination
# ---------------------------------------------------------------------------


class TestGetProductsPagination:
    """Pagination tests for get_products()."""

    def test_single_page_returns_all_products(self):
        """A response with <100 results should return them all without a second request."""
        connector = _make_connector()
        mock_resp = _mock_response(200, LISTINGS_SINGLE_PAGE)

        call_count = 0

        async def request_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_resp
            # Should not be called again
            return _mock_response(200, {"count": 0, "results": []})

        with patch.object(connector, "_request", new=AsyncMock(side_effect=request_side_effect)):
            products = asyncio.run(connector.get_products())

        assert len(products) == 2
        assert call_count == 1  # Only one page fetched

    def test_two_page_pagination(self):
        """Full page (100 results) triggers a second request; partial page ends pagination."""
        connector = _make_connector()
        mock_page1 = _mock_response(200, LISTINGS_PAGE_1)
        mock_page2 = _mock_response(200, LISTINGS_PAGE_2)

        with patch.object(
            connector,
            "_request",
            new=AsyncMock(side_effect=[mock_page1, mock_page2]),
        ):
            products = asyncio.run(connector.get_products())

        # 100 from page 1 + 50 from page 2
        assert len(products) == 150

    def test_empty_results_returns_empty_list(self):
        """Empty results on first page returns an empty list without error."""
        connector = _make_connector()
        mock_resp = _mock_response(200, {"count": 0, "results": []})

        with patch.object(connector, "_request", new=AsyncMock(return_value=mock_resp)):
            products = asyncio.run(connector.get_products())

        assert products == []

    def test_inactive_listings_filtered_out(self):
        """Listings with state != 'active' must not appear in results."""
        connector = _make_connector()

        mixed_response = {
            "count": 2,
            "results": [
                {
                    "listing_id": 555000001,
                    "state": "inactive",
                    "title": "Old Inactive Item",
                    "price": {"amount": 1000, "divisor": 100, "currency_code": "USD"},
                    "url": "https://www.etsy.com/listing/555000001/old",
                },
                {
                    "listing_id": 555000002,
                    "state": "active",
                    "title": "Active Item",
                    "price": {"amount": 2000, "divisor": 100, "currency_code": "USD"},
                    "url": "https://www.etsy.com/listing/555000002/active",
                },
            ],
        }
        mock_resp = _mock_response(200, mixed_response)

        with patch.object(connector, "_request", new=AsyncMock(return_value=mock_resp)):
            products = asyncio.run(connector.get_products())

        assert len(products) == 1
        assert products[0].platform_product_id == "555000002"


# ---------------------------------------------------------------------------
# get_competitor_prices (via _get_competitor_prices_impl)
# ---------------------------------------------------------------------------


class TestGetCompetitorPrices:
    """Tests for _get_competitor_prices_impl()."""

    def test_keyword_search_returns_competitors(self):
        """Competitor search returns list of CompetitorProduct instances."""
        connector = _make_connector()
        product = _sample_product()
        mock_resp = _mock_response(200, COMPETITOR_SEARCH_RESULTS)

        with patch.object(connector, "_request", new=AsyncMock(return_value=mock_resp)):
            competitors = asyncio.run(connector._get_competitor_prices_impl(product))

        # COMPETITOR_SEARCH_RESULTS has 4 entries but 1 is the seller's own listing
        # (url contains /shop/99999999/) — should be filtered out, leaving 3
        assert len(competitors) == 3

    def test_own_shop_filtered_out(self):
        """Listings matching the seller's own shop_id in the URL must be excluded."""
        connector = _make_connector()
        product = _sample_product()

        own_shop_only = {
            "count": 1,
            "results": [
                {
                    "listing_id": 100000001,
                    "state": "active",
                    "title": "My Own Listing",
                    "price": {"amount": 2800, "divisor": 100, "currency_code": "USD"},
                    "url": f"https://www.etsy.com/shop/{_SHOP_ID}/listing/100000001",
                }
            ],
        }
        mock_resp = _mock_response(200, own_shop_only)

        with patch.object(connector, "_request", new=AsyncMock(return_value=mock_resp)):
            competitors = asyncio.run(connector._get_competitor_prices_impl(product))

        assert competitors == []

    def test_competitor_prices_have_correct_extra_fields(self):
        """Competitor results must include search_method=keyword in extra."""
        connector = _make_connector()
        product = _sample_product()
        mock_resp = _mock_response(200, COMPETITOR_SEARCH_RESULTS)

        with patch.object(connector, "_request", new=AsyncMock(return_value=mock_resp)):
            competitors = asyncio.run(connector._get_competitor_prices_impl(product))

        for comp in competitors:
            assert comp.extra.get("search_method") == "keyword"
            assert comp.extra.get("match_type") == "approximate"
            assert comp.is_fulfilled_by_platform is False
            assert comp.condition == "new"

    def test_price_conversion_in_competitor_results(self):
        """Competitor prices use amount/divisor conversion correctly."""
        connector = _make_connector()
        product = _sample_product()

        response = {
            "count": 1,
            "results": [
                {
                    "listing_id": 400000001,
                    "state": "active",
                    "title": "Competitor Mug",
                    "price": {"amount": 1999, "divisor": 100, "currency_code": "USD"},
                    "url": "https://www.etsy.com/listing/400000001/competitor-mug",
                }
            ],
        }
        mock_resp = _mock_response(200, response)

        with patch.object(connector, "_request", new=AsyncMock(return_value=mock_resp)):
            competitors = asyncio.run(connector._get_competitor_prices_impl(product))

        assert len(competitors) == 1
        assert competitors[0].price == 19.99

    def test_capped_at_ten_results(self):
        """Competitor results are capped at _MAX_COMPETITOR_RESULTS (10)."""
        connector = _make_connector()
        product = _sample_product()

        # 15 competitor listings — connector should return at most 10
        many_competitors = {
            "count": 15,
            "results": [
                {
                    "listing_id": 500000000 + i,
                    "state": "active",
                    "title": f"Competitor Mug {i}",
                    "price": {"amount": 2000 + i * 100, "divisor": 100, "currency_code": "USD"},
                    "url": f"https://www.etsy.com/listing/{500000000 + i}/mug-{i}",
                }
                for i in range(15)
            ],
        }
        mock_resp = _mock_response(200, many_competitors)

        with patch.object(connector, "_request", new=AsyncMock(return_value=mock_resp)):
            competitors = asyncio.run(connector._get_competitor_prices_impl(product))

        assert len(competitors) == 10

    def test_zero_price_competitors_skipped(self):
        """Competitor listings with price=0 after conversion are not included."""
        connector = _make_connector()
        product = _sample_product()

        zero_price_response = {
            "count": 1,
            "results": [
                {
                    "listing_id": 600000001,
                    "state": "active",
                    "title": "Free Item",
                    "price": {"amount": 0, "divisor": 100, "currency_code": "USD"},
                    "url": "https://www.etsy.com/listing/600000001/free",
                }
            ],
        }
        mock_resp = _mock_response(200, zero_price_response)

        with patch.object(connector, "_request", new=AsyncMock(return_value=mock_resp)):
            competitors = asyncio.run(connector._get_competitor_prices_impl(product))

        assert competitors == []


# ---------------------------------------------------------------------------
# Daily rate limit
# ---------------------------------------------------------------------------


class TestDailyRateLimit:
    """Tests for _check_daily_rate_limit()."""

    def test_rate_limit_raises_when_count_exceeds_9500(self):
        """_check_daily_rate_limit raises PlatformRateLimitError when daily count >= 9500."""
        mock_db = MagicMock()
        mock_result = MagicMock()
        mock_result.count = 9500
        chain = MagicMock()
        chain.select.return_value = chain
        chain.eq.return_value = chain
        chain.gte.return_value = chain
        chain.execute.return_value = mock_result
        mock_db.table.return_value = chain

        connector = _make_connector(db=mock_db)

        with pytest.raises(PlatformRateLimitError, match="daily API limit"):
            asyncio.run(connector._check_daily_rate_limit())

    def test_rate_limit_passes_when_count_below_9500(self):
        """_check_daily_rate_limit passes without error when count < 9500."""
        mock_db = MagicMock()
        mock_result = MagicMock()
        mock_result.count = 100
        chain = MagicMock()
        chain.select.return_value = chain
        chain.eq.return_value = chain
        chain.gte.return_value = chain
        chain.execute.return_value = mock_result
        mock_db.table.return_value = chain

        connector = _make_connector(db=mock_db)

        # Should not raise
        asyncio.run(connector._check_daily_rate_limit())

    def test_rate_limit_skipped_when_no_db(self):
        """Without a db client, _check_daily_rate_limit is a no-op."""
        connector = _make_connector(db=None)
        # Must not raise
        asyncio.run(connector._check_daily_rate_limit())

    def test_db_error_does_not_block_pipeline(self):
        """DB error during rate limit check is swallowed — pipeline must continue."""
        mock_db = MagicMock()
        mock_db.table.side_effect = Exception("DB connection lost")

        connector = _make_connector(db=mock_db)
        # Must not raise
        asyncio.run(connector._check_daily_rate_limit())


# ---------------------------------------------------------------------------
# apply_price
# ---------------------------------------------------------------------------


class TestApplyPrice:
    """Tests for _apply_price_impl()."""

    def test_success_on_200(self):
        """200 from PATCH returns True."""
        connector = _make_connector()
        product = _sample_product()
        mock_resp = _mock_response(200, PATCH_SUCCESS)

        with patch.object(connector, "_request", new=AsyncMock(return_value=mock_resp)):
            result = asyncio.run(connector._apply_price_impl(product, 26.00))

        assert result is True

    def test_success_on_204(self):
        """204 from PATCH also returns True."""
        connector = _make_connector()
        product = _sample_product()
        mock_resp = _mock_response(204, {}, is_success=True)

        with patch.object(connector, "_request", new=AsyncMock(return_value=mock_resp)):
            result = asyncio.run(connector._apply_price_impl(product, 26.00))

        assert result is True

    def test_api_error_on_400(self):
        """400 from PATCH raises PlatformAPIError."""
        connector = _make_connector()
        product = _sample_product()
        mock_resp = _mock_response(400, {"error": "invalid price"}, is_success=False)

        with patch.object(connector, "_request", new=AsyncMock(return_value=mock_resp)):
            with pytest.raises(PlatformAPIError) as exc_info:
                asyncio.run(connector._apply_price_impl(product, 26.00))

        assert exc_info.value.status_code == 400

    def test_api_error_on_500(self):
        """500 from PATCH raises PlatformAPIError."""
        connector = _make_connector()
        product = _sample_product()
        mock_resp = _mock_response(500, {}, is_success=False)

        with patch.object(connector, "_request", new=AsyncMock(return_value=mock_resp)):
            with pytest.raises(PlatformAPIError):
                asyncio.run(connector._apply_price_impl(product, 26.00))

    def test_price_rounded_to_two_decimals(self):
        """Price is rounded to 2 decimal places before sending to Etsy."""
        connector = _make_connector()
        product = _sample_product()
        mock_resp = _mock_response(200, PATCH_SUCCESS)
        captured_calls = []

        async def request_capture(*args, **kwargs):
            captured_calls.append(kwargs.get("json", {}))
            return mock_resp

        with patch.object(connector, "_request", new=AsyncMock(side_effect=request_capture)):
            asyncio.run(connector._apply_price_impl(product, 26.999))

        assert len(captured_calls) == 1
        assert captured_calls[0]["price"] == 27.0


# ---------------------------------------------------------------------------
# Auto-refresh (401 → refresh → retry)
# ---------------------------------------------------------------------------


class TestAutoRefresh:
    """Tests for the automatic token refresh flow inside _request()."""

    def test_401_triggers_refresh_and_retry(self):
        """First 401 should call _refresh_access_token() and retry the original request."""
        connector = _make_connector()
        product = _sample_product()

        resp_401 = _mock_response(401, {"error": "expired"}, is_success=False)
        resp_ok = _mock_response(200, PATCH_SUCCESS)

        call_count = 0

        async def http_side_effect(method, url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return resp_401
            return resp_ok

        async def mock_refresh():
            connector._credentials["access_token"] = "new-refreshed-token"

        mock_http_client = AsyncMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_http_client.request = AsyncMock(side_effect=http_side_effect)

        with patch("platforms.etsy.httpx.AsyncClient", return_value=mock_http_client), \
             patch.object(connector, "_refresh_access_token", new=AsyncMock(side_effect=mock_refresh)), \
             patch.object(connector, "_check_daily_rate_limit", new=AsyncMock()), \
             patch.object(connector, "_record_api_call"):
            asyncio.run(connector._apply_price_impl(product, 26.00))

        assert call_count == 2
        assert connector._credentials["access_token"] == "new-refreshed-token"

    def test_second_401_after_refresh_raises_auth_error(self):
        """Two consecutive 401s (after token refresh) must raise PlatformAuthError."""
        connector = _make_connector()

        resp_401 = _mock_response(401, {"error": "expired"}, is_success=False)

        mock_http_client = AsyncMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_http_client.request = AsyncMock(return_value=resp_401)

        async def mock_refresh():
            # Refresh "succeeds" but the new token is also invalid
            pass

        with patch("platforms.etsy.httpx.AsyncClient", return_value=mock_http_client), \
             patch.object(connector, "_refresh_access_token", new=AsyncMock(side_effect=mock_refresh)), \
             patch.object(connector, "_check_daily_rate_limit", new=AsyncMock()), \
             patch.object(connector, "_record_api_call"):
            with pytest.raises(PlatformAuthError):
                asyncio.run(
                    connector._request("GET", "https://openapi.etsy.com/v3/application/users/me")
                )

    def test_refresh_token_expired_raises_auth_error(self):
        """If _refresh_access_token itself raises PlatformAuthError, it propagates."""
        connector = _make_connector()

        resp_401 = _mock_response(401, {"error": "expired"}, is_success=False)

        mock_http_client = AsyncMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_http_client.request = AsyncMock(return_value=resp_401)

        with patch("platforms.etsy.httpx.AsyncClient", return_value=mock_http_client), \
             patch.object(
                 connector,
                 "_refresh_access_token",
                 new=AsyncMock(side_effect=PlatformAuthError("refresh failed", platform="etsy")),
             ), \
             patch.object(connector, "_check_daily_rate_limit", new=AsyncMock()), \
             patch.object(connector, "_record_api_call"):
            with pytest.raises(PlatformAuthError):
                asyncio.run(
                    connector._request("GET", "https://openapi.etsy.com/v3/application/users/me")
                )


# ---------------------------------------------------------------------------
# _refresh_access_token directly
# ---------------------------------------------------------------------------


class TestRefreshAccessToken:
    """Unit tests for _refresh_access_token()."""

    def test_successful_refresh_updates_credential_in_memory(self):
        """Successful token refresh updates _credentials['access_token']."""
        connector = _make_connector()
        original_token = connector._credentials["access_token"]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.json = MagicMock(return_value=TOKEN_REFRESH_SUCCESS)

        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.post = AsyncMock(return_value=mock_resp)

        with patch("platforms.etsy.httpx.AsyncClient", return_value=mock_http):
            asyncio.run(connector._refresh_access_token())

        assert connector._credentials["access_token"] == TOKEN_REFRESH_SUCCESS["access_token"]
        assert connector._credentials["access_token"] != original_token

    def test_400_raises_platform_auth_error(self):
        """400 from token endpoint raises PlatformAuthError."""
        connector = _make_connector()

        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.is_success = False
        mock_resp.json = MagicMock(return_value={"error": "invalid_grant"})
        mock_resp.text = "invalid_grant"

        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.post = AsyncMock(return_value=mock_resp)

        with patch("platforms.etsy.httpx.AsyncClient", return_value=mock_http):
            with pytest.raises(PlatformAuthError) as exc_info:
                asyncio.run(connector._refresh_access_token())

        assert exc_info.value.status_code == 400

    def test_401_raises_platform_auth_error(self):
        """401 from token endpoint raises PlatformAuthError."""
        connector = _make_connector()

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.is_success = False
        mock_resp.text = "unauthorized"

        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.post = AsyncMock(return_value=mock_resp)

        with patch("platforms.etsy.httpx.AsyncClient", return_value=mock_http):
            with pytest.raises(PlatformAuthError) as exc_info:
                asyncio.run(connector._refresh_access_token())

        assert exc_info.value.status_code == 401
