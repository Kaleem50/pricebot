"""
tests/unit/test_price_cache.py — Competitor Price Cache Tests

Tests for:
  BasePlatformConnector._read_price_cache()    — cache hit / stale / error paths
  BasePlatformConnector._write_price_cache()   — successful write and silent failure
  BasePlatformConnector.get_competitor_prices() — cache-first flow
  AmazonConnector._get_pricing_batch()          — multi-ASIN batch API call
  AmazonConnector.get_competitor_prices_bulk()  — concurrent batch + cache

All network calls and DB queries are mocked.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Env setup (must precede local imports)
# ---------------------------------------------------------------------------

import os

os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", "a" * 64)

from core.repricing_engine import CompetitorProduct, MyProduct
from platforms.amazon import AmazonConnector
from platforms.exceptions import PlatformRateLimitError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW_UTC = datetime.now(timezone.utc)
_FRESH_TS = (_NOW_UTC - timedelta(minutes=5)).isoformat()   # 5 min ago — fresh
_STALE_TS = (_NOW_UTC - timedelta(minutes=20)).isoformat()  # 20 min ago — stale


def _make_credentials() -> dict[str, str]:
    return {
        "refresh_token": "Atzr|test_token",
        "client_id": "amzn1.application-oa2-client.test",
        "client_secret": "test_secret",
        "marketplace_id": "ATVPDKIKX0DER",
        "merchant_id": "MERCHANT123",
    }


def _make_product(
    product_id: str = "prod-001",
    asin: str = "B001TESTXX",
    price: float = 29.99,
) -> MyProduct:
    return MyProduct(
        product_id=product_id,
        platform_product_id=asin,
        platform_sku=f"SKU-{asin}",
        title="Test Product",
        platform="amazon",
        current_price=price,
        cost=10.0,
        min_margin_floor=5.0,
        user_id="user-uuid-123",
        platform_context={},
        metadata={},
    )


def _make_competitor(price: float = 27.99) -> CompetitorProduct:
    return CompetitorProduct(
        price=price,
        platform="amazon",
        is_fulfilled_by_platform=True,
        condition="new",
        extra={"buy_box_price": price, "fba_competitor_count": 1},
    )


def _make_db_mock() -> MagicMock:
    """Return a Supabase mock with chainable .table().select().eq().execute()."""
    db = MagicMock()
    table = MagicMock()
    db.table = MagicMock(return_value=table)
    table.select = MagicMock(return_value=table)
    table.update = MagicMock(return_value=table)
    table.eq = MagicMock(return_value=table)
    table.execute = MagicMock(return_value=MagicMock(data=[]))
    return db, table


@pytest.fixture(autouse=True)
def clear_token_cache():
    """Ensure LWA token cache is empty before and after every test."""
    AmazonConnector._token_cache.clear()
    yield
    AmazonConnector._token_cache.clear()


# ---------------------------------------------------------------------------
# Helper: mock httpx so LWA doesn't need a real network call
# ---------------------------------------------------------------------------

def _mock_httpx_client_for_lwa(token: str = "access-token-123") -> tuple:
    """Return a mock httpx AsyncClient that returns a fake LWA token."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    lwa_resp = MagicMock()
    lwa_resp.status_code = 200
    lwa_resp.json = MagicMock(
        return_value={"access_token": token, "expires_in": 3600}
    )
    mock_client.post = AsyncMock(return_value=lwa_resp)
    return mock_client, lwa_resp


# ===========================================================================
# Tests: _read_price_cache
# ===========================================================================


class TestReadPriceCache:
    """_read_price_cache returns fresh entries, skips stale, handles no db."""

    def test_returns_none_when_no_db(self):
        """No db= → caching disabled → always None."""
        connector = AmazonConnector(
            credentials=_make_credentials(), user_id="user-1"
        )
        product = _make_product()
        result = connector._read_price_cache(product)
        assert result is None

    def test_returns_none_when_row_not_found(self):
        db, table = _make_db_mock()
        table.execute = MagicMock(return_value=MagicMock(data=[]))
        connector = AmazonConnector(
            credentials=_make_credentials(), user_id="user-1", db=db
        )
        result = connector._read_price_cache(_make_product())
        assert result is None

    def test_returns_none_when_cached_at_is_null(self):
        db, table = _make_db_mock()
        table.execute = MagicMock(
            return_value=MagicMock(
                data=[{"competitor_prices_cached_at": None, "competitor_prices_cache": []}]
            )
        )
        connector = AmazonConnector(
            credentials=_make_credentials(), user_id="user-1", db=db
        )
        result = connector._read_price_cache(_make_product())
        assert result is None

    def test_returns_none_when_cache_is_stale(self):
        db, table = _make_db_mock()
        competitor_data = [_make_competitor().model_dump()]
        table.execute = MagicMock(
            return_value=MagicMock(
                data=[{
                    "competitor_prices_cached_at": _STALE_TS,
                    "competitor_prices_cache": competitor_data,
                }]
            )
        )
        connector = AmazonConnector(
            credentials=_make_credentials(), user_id="user-1", db=db
        )
        result = connector._read_price_cache(_make_product())
        assert result is None  # 20 min old → stale

    def test_returns_competitors_when_cache_is_fresh(self):
        db, table = _make_db_mock()
        competitor = _make_competitor(price=25.00)
        table.execute = MagicMock(
            return_value=MagicMock(
                data=[{
                    "competitor_prices_cached_at": _FRESH_TS,
                    "competitor_prices_cache": [competitor.model_dump()],
                }]
            )
        )
        connector = AmazonConnector(
            credentials=_make_credentials(), user_id="user-1", db=db
        )
        result = connector._read_price_cache(_make_product())
        assert result is not None
        assert len(result) == 1
        assert result[0].price == 25.00

    def test_returns_none_on_db_exception(self):
        """DB error → cache miss (do not propagate exception)."""
        db, table = _make_db_mock()
        table.execute = MagicMock(side_effect=RuntimeError("DB connection error"))
        connector = AmazonConnector(
            credentials=_make_credentials(), user_id="user-1", db=db
        )
        result = connector._read_price_cache(_make_product())
        assert result is None  # Error swallowed — falls through to live fetch

    def test_db_select_filters_by_user_id(self):
        """Must include eq(user_id) filter to prevent cross-tenant data leak."""
        db, table = _make_db_mock()
        connector = AmazonConnector(
            credentials=_make_credentials(), user_id="user-sentinel", db=db
        )
        connector._read_price_cache(_make_product())

        eq_calls_args = [str(c) for c in table.eq.call_args_list]
        assert any("user_id" in c and "user-sentinel" in c for c in eq_calls_args)


# ===========================================================================
# Tests: _write_price_cache
# ===========================================================================


class TestWritePriceCache:
    """_write_price_cache writes timestamp + JSON, silently skips on error."""

    def test_no_op_when_no_db(self):
        connector = AmazonConnector(
            credentials=_make_credentials(), user_id="user-1"
        )
        # Should not raise
        connector._write_price_cache(_make_product(), [_make_competitor()])

    def test_updates_both_columns(self):
        db, table = _make_db_mock()
        connector = AmazonConnector(
            credentials=_make_credentials(), user_id="user-1", db=db
        )
        competitor = _make_competitor(price=30.00)
        connector._write_price_cache(_make_product(), [competitor])

        table.update.assert_called_once()
        payload = table.update.call_args[0][0]
        assert "competitor_prices_cached_at" in payload
        assert "competitor_prices_cache" in payload
        assert len(payload["competitor_prices_cache"]) == 1
        assert payload["competitor_prices_cache"][0]["price"] == 30.00

    def test_silently_swallows_db_error(self):
        """Cache write failure must NEVER block the repricing pipeline."""
        db, table = _make_db_mock()
        table.execute = MagicMock(side_effect=RuntimeError("write failed"))
        connector = AmazonConnector(
            credentials=_make_credentials(), user_id="user-1", db=db
        )
        # Should not raise
        connector._write_price_cache(_make_product(), [_make_competitor()])

    def test_write_filters_by_user_id(self):
        db, table = _make_db_mock()
        connector = AmazonConnector(
            credentials=_make_credentials(), user_id="user-sentinel", db=db
        )
        connector._write_price_cache(_make_product(), [_make_competitor()])
        eq_calls = [str(c) for c in table.eq.call_args_list]
        assert any("user_id" in c and "user-sentinel" in c for c in eq_calls)


# ===========================================================================
# Tests: get_competitor_prices (cache-first)
# ===========================================================================


class TestGetCompetitorPricesCacheFirst:
    """get_competitor_prices() returns from cache when fresh, otherwise fetches."""

    @pytest.mark.asyncio
    async def test_returns_from_cache_without_api_call(self):
        """When cache is fresh, no SP-API call should be made."""
        db, table = _make_db_mock()
        competitor = _make_competitor(price=22.00)
        table.execute = MagicMock(
            return_value=MagicMock(
                data=[{
                    "competitor_prices_cached_at": _FRESH_TS,
                    "competitor_prices_cache": [competitor.model_dump()],
                }]
            )
        )

        connector = AmazonConnector(
            credentials=_make_credentials(), user_id="user-1", db=db
        )
        with patch("platforms.amazon.httpx.AsyncClient") as mock_httpx:
            result = await connector.get_competitor_prices(_make_product())

        mock_httpx.assert_not_called()  # No HTTP calls made
        assert len(result) == 1
        assert result[0].price == 22.00

    @pytest.mark.asyncio
    async def test_fetches_and_writes_cache_when_stale(self):
        """Stale cache → API call → cache is updated."""
        db, table = _make_db_mock()

        # Read returns stale entry
        stale_data = {
            "competitor_prices_cached_at": _STALE_TS,
            "competitor_prices_cache": [_make_competitor(price=19.99).model_dump()],
        }
        fresh_competitor = _make_competitor(price=24.00)

        call_count = [0]

        def select_side_effect(*args, **kwargs):
            call_count[0] += 1
            table.execute = MagicMock(return_value=MagicMock(data=[stale_data]))
            return table

        table.select = MagicMock(side_effect=select_side_effect)

        connector = AmazonConnector(
            credentials=_make_credentials(), user_id="user-1", db=db
        )

        mock_client, _ = _mock_httpx_client_for_lwa()

        # Mock the competitive pricing response
        pricing_resp = MagicMock()
        pricing_resp.status_code = 200
        pricing_resp.is_success = True
        pricing_resp.json = MagicMock(return_value={
            "payload": [{
                "ASIN": "B001TESTXX",
                "status": "Success",
                "Product": {
                    "CompetitivePricing": {
                        "CompetitivePrices": [{
                            "CompetitivePriceId": "1",
                            "condition": "New",
                            "Price": {"LandedPrice": {"Amount": 24.00}},
                            "belongsToRequester": False,
                        }]
                    }
                }
            }]
        })
        mock_client.get = AsyncMock(return_value=pricing_resp)

        write_called = []

        original_write = connector._write_price_cache

        def capture_write(product, prices):
            write_called.append(prices)
            # Don't actually write (table mock is fragile in this test)

        connector._write_price_cache = capture_write

        with patch("platforms.amazon.httpx.AsyncClient", return_value=mock_client):
            with patch("asyncio.sleep", new=AsyncMock()):
                result = await connector.get_competitor_prices(_make_product())

        assert len(result) == 1
        assert result[0].price == 24.00
        assert len(write_called) == 1  # Cache was updated


# ===========================================================================
# Tests: _get_pricing_batch
# ===========================================================================


class TestGetPricingBatch:
    """_get_pricing_batch fetches prices for multiple ASINs in one API call."""

    @pytest.mark.asyncio
    async def test_returns_empty_dict_for_empty_products(self):
        connector = AmazonConnector(
            credentials=_make_credentials(), user_id="user-1"
        )
        result = await connector._get_pricing_batch([])
        assert result == {}

    @pytest.mark.asyncio
    async def test_maps_asin_results_to_product_ids(self):
        """ASIN in API response must map back to the correct product_id."""
        product_a = _make_product("prod-A", "B001AAAAA", 30.0)
        product_b = _make_product("prod-B", "B002BBBBB", 50.0)

        mock_client, _ = _mock_httpx_client_for_lwa()

        pricing_resp = MagicMock()
        pricing_resp.status_code = 200
        pricing_resp.is_success = True
        pricing_resp.json = MagicMock(return_value={
            "payload": [
                {
                    "ASIN": "B001AAAAA",
                    "status": "Success",
                    "Product": {
                        "CompetitivePricing": {
                            "CompetitivePrices": [{
                                "CompetitivePriceId": "1",
                                "condition": "New",
                                "Price": {"LandedPrice": {"Amount": 28.00}},
                                "belongsToRequester": False,
                            }]
                        }
                    },
                },
                {
                    "ASIN": "B002BBBBB",
                    "status": "Success",
                    "Product": {
                        "CompetitivePricing": {
                            "CompetitivePrices": [{
                                "CompetitivePriceId": "1",
                                "condition": "New",
                                "Price": {"LandedPrice": {"Amount": 47.00}},
                                "belongsToRequester": False,
                            }]
                        }
                    },
                },
            ]
        })
        mock_client.get = AsyncMock(return_value=pricing_resp)

        connector = AmazonConnector(
            credentials=_make_credentials(), user_id="user-1"
        )

        with patch("platforms.amazon.httpx.AsyncClient", return_value=mock_client):
            result = await connector._get_pricing_batch([product_a, product_b])

        assert "prod-A" in result
        assert "prod-B" in result
        assert result["prod-A"][0].price == 28.00
        assert result["prod-B"][0].price == 47.00

    @pytest.mark.asyncio
    async def test_raises_rate_limit_error_on_429(self):
        product = _make_product()
        mock_client, _ = _mock_httpx_client_for_lwa()

        rate_resp = MagicMock()
        rate_resp.status_code = 429
        rate_resp.is_success = False
        rate_resp.headers = {"retry-after": "2"}
        mock_client.get = AsyncMock(return_value=rate_resp)

        connector = AmazonConnector(
            credentials=_make_credentials(), user_id="user-1"
        )
        with patch("platforms.amazon.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(PlatformRateLimitError) as exc_info:
                await connector._get_pricing_batch([product])

        assert exc_info.value.retry_after == 2

    @pytest.mark.asyncio
    async def test_products_with_no_api_result_get_empty_list(self):
        """ASINs not in API payload map to empty competitor list."""
        product = _make_product("prod-X", "B00XMISSING", 40.0)
        mock_client, _ = _mock_httpx_client_for_lwa()

        pricing_resp = MagicMock()
        pricing_resp.status_code = 200
        pricing_resp.is_success = True
        pricing_resp.json = MagicMock(return_value={"payload": []})
        mock_client.get = AsyncMock(return_value=pricing_resp)

        connector = AmazonConnector(
            credentials=_make_credentials(), user_id="user-1"
        )
        with patch("platforms.amazon.httpx.AsyncClient", return_value=mock_client):
            result = await connector._get_pricing_batch([product])

        assert "prod-X" in result
        assert result["prod-X"] == []

    @pytest.mark.asyncio
    async def test_asin_list_sent_as_comma_separated_param(self):
        """Verifies all ASINs are included in the single API request."""
        products = [
            _make_product(f"prod-{i}", f"B00ASIN{i:04d}", 10.0 * i)
            for i in range(1, 4)
        ]
        mock_client, _ = _mock_httpx_client_for_lwa()

        pricing_resp = MagicMock()
        pricing_resp.status_code = 200
        pricing_resp.is_success = True
        pricing_resp.json = MagicMock(return_value={"payload": []})
        mock_client.get = AsyncMock(return_value=pricing_resp)

        connector = AmazonConnector(
            credentials=_make_credentials(), user_id="user-1"
        )
        with patch("platforms.amazon.httpx.AsyncClient", return_value=mock_client):
            await connector._get_pricing_batch(products)

        # The GET call's params should contain all three ASINs in `Asins`
        get_call_kwargs = mock_client.get.call_args
        params_sent = get_call_kwargs[1].get("params") or get_call_kwargs[0][1]
        asins_param: str = params_sent["Asins"]
        for p in products:
            assert p.platform_product_id in asins_param


# ===========================================================================
# Tests: get_competitor_prices_bulk
# ===========================================================================


class TestGetCompetitorPricesBulk:
    """get_competitor_prices_bulk: concurrent batching, cache, error isolation."""

    @pytest.mark.asyncio
    async def test_returns_empty_dict_for_no_products(self):
        connector = AmazonConnector(
            credentials=_make_credentials(), user_id="user-1"
        )
        result = await connector.get_competitor_prices_bulk([])
        assert result == {}

    @pytest.mark.asyncio
    async def test_all_from_cache_makes_no_api_calls(self):
        """When every product has a fresh cache entry, zero API calls happen."""
        db, table = _make_db_mock()
        competitor = _make_competitor(price=19.00)
        table.execute = MagicMock(
            return_value=MagicMock(
                data=[{
                    "competitor_prices_cached_at": _FRESH_TS,
                    "competitor_prices_cache": [competitor.model_dump()],
                }]
            )
        )

        connector = AmazonConnector(
            credentials=_make_credentials(), user_id="user-1", db=db
        )
        products = [_make_product(f"prod-{i}", f"B00ASIN{i:04d}", 20.0) for i in range(3)]

        with patch("platforms.amazon.httpx.AsyncClient") as mock_httpx:
            result = await connector.get_competitor_prices_bulk(products)

        mock_httpx.assert_not_called()
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_groups_into_batches_of_20(self):
        """25 products → 2 batches (20 + 5) → 2 API calls."""
        connector = AmazonConnector(
            credentials=_make_credentials(), user_id="user-1"
        )
        products = [
            _make_product(f"prod-{i}", f"B00ASIN{i:04d}", 10.0)
            for i in range(25)
        ]

        batch_call_asins: list[str] = []

        async def fake_get_pricing_batch(batch: list) -> dict:
            batch_call_asins.append(",".join(p.platform_product_id for p in batch))
            return {p.product_id: [_make_competitor()] for p in batch}

        connector._get_pricing_batch = fake_get_pricing_batch

        with patch("asyncio.sleep", new=AsyncMock()):
            result = await connector.get_competitor_prices_bulk(products)

        assert len(batch_call_asins) == 2  # 2 batch API calls
        assert len(result) == 25

    @pytest.mark.asyncio
    async def test_failed_batch_does_not_prevent_other_batches(self):
        """One batch erroring should not cancel the other batches."""
        connector = AmazonConnector(
            credentials=_make_credentials(), user_id="user-1"
        )
        products = [
            _make_product(f"prod-{i}", f"B00ASIN{i:04d}", 10.0)
            for i in range(40)  # 2 batches of 20
        ]

        call_number = [0]

        async def fake_get_pricing_batch(batch: list) -> dict:
            call_number[0] += 1
            if call_number[0] == 1:
                raise PlatformRateLimitError(
                    "Simulated rate limit", platform="amazon"
                )
            return {p.product_id: [_make_competitor()] for p in batch}

        connector._get_pricing_batch = fake_get_pricing_batch

        with patch("asyncio.sleep", new=AsyncMock()):
            result = await connector.get_competitor_prices_bulk(products)

        # First batch failed → its 20 products absent from results
        # Second batch succeeded → its 20 products present
        assert len(result) == 20

    @pytest.mark.asyncio
    async def test_writes_cache_for_fresh_results(self):
        """Freshly fetched results must be written to the Supabase cache."""
        db, table = _make_db_mock()

        # DB read returns no cache (empty data) so all products need fetch
        table.execute = MagicMock(return_value=MagicMock(data=[]))

        connector = AmazonConnector(
            credentials=_make_credentials(), user_id="user-1", db=db
        )
        product = _make_product("prod-cache-write", "B00CACHEWRITE", 30.0)

        write_calls = []

        def capture_write(p, prices):
            write_calls.append((p.product_id, prices))

        connector._write_price_cache = capture_write

        async def fake_get_pricing_batch(batch):
            return {"prod-cache-write": [_make_competitor(price=28.00)]}

        connector._get_pricing_batch = fake_get_pricing_batch

        with patch("asyncio.sleep", new=AsyncMock()):
            result = await connector.get_competitor_prices_bulk([product])

        assert result["prod-cache-write"][0].price == 28.00
        assert len(write_calls) == 1
        assert write_calls[0][0] == "prod-cache-write"

    @pytest.mark.asyncio
    async def test_mixed_cached_and_stale_products(self):
        """Products with fresh cache are not fetched; stale ones are."""
        db, _ = _make_db_mock()
        competitor_cached = _make_competitor(price=10.00)

        call_seq = [0]

        def db_execute_side_effect(*args, **kwargs):
            # First call (fresh product), second call (stale product)
            call_seq[0] += 1
            if call_seq[0] <= 2:  # 2 .eq() chains per product
                return MagicMock(
                    data=[{
                        "competitor_prices_cached_at": _FRESH_TS,
                        "competitor_prices_cache": [competitor_cached.model_dump()],
                    }]
                )
            return MagicMock(data=[])

        # Keep it simple: override _read_price_cache directly
        fresh_product = _make_product("fresh-prod", "B00FRESH001", 20.0)
        stale_product = _make_product("stale-prod", "B00STALE001", 30.0)

        connector = AmazonConnector(
            credentials=_make_credentials(), user_id="user-1"
        )

        def mock_read_cache(product):
            if product.product_id == "fresh-prod":
                return [competitor_cached]
            return None  # stale

        batch_calls = []

        async def fake_batch(batch):
            batch_calls.append([p.product_id for p in batch])
            return {p.product_id: [_make_competitor(price=28.00)] for p in batch}

        connector._read_price_cache = mock_read_cache
        connector._get_pricing_batch = fake_batch
        connector._write_price_cache = lambda p, c: None  # suppress write

        with patch("asyncio.sleep", new=AsyncMock()):
            result = await connector.get_competitor_prices_bulk(
                [fresh_product, stale_product]
            )

        # Fresh product served from cache
        assert result["fresh-prod"][0].price == 10.00
        # Stale product fetched via API
        assert result["stale-prod"][0].price == 28.00
        # Only one batch was made (for the stale product)
        assert batch_calls == [["stale-prod"]]
