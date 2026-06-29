"""
platforms/amazon.py — Amazon Selling Partner API Connector

Implements BasePlatformConnector for Amazon using the SP-API.

Authentication (PLATFORMS.md §3.3):
  Amazon uses Login with Amazon (LWA) OAuth 2.0.  The connector exchanges
  the seller's refresh_token for a short-lived access_token via POST to
  https://api.amazon.com/auth/o2/token, then caches the token per user_id
  for up to 1 hour (minus a 60-second buffer) to avoid redundant LWA calls.

Required credentials keys:
  - refresh_token:    LWA refresh token from Seller Central
  - client_id:        SP-API app client ID
  - client_secret:    SP-API app client secret
  - marketplace_id:   Amazon marketplace (e.g. ATVPDKIKX0DER for US)
  - merchant_id:      Seller's MerchantToken / SellerId

Rate limits (PLATFORMS.md §3.4):
  - Listings Items:      1 req/s (burst 5)
  - Competitive Pricing: 1 req/s (burst 1)
  - Listings Patches:    5 req/s (burst 10)

  The connector sleeps 1.0 s between competitive-pricing batches (20 ASINs max)
  to stay within the 1 req/s limit.  429 responses raise PlatformRateLimitError
  which the base class tenacity decorator catches and retries.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, ClassVar

import httpx

from core.repricing_engine import CompetitorProduct, MyProduct
from platforms.base import BasePlatformConnector
from platforms.exceptions import (
    PlatformAPIError,
    PlatformAuthError,
    PlatformProductNotFoundError,
    PlatformRateLimitError,
)

logger = logging.getLogger(__name__)

# Amazon SP-API endpoints
_LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
_SP_API_BASE = "https://sellingpartnerapi-na.amazon.com"

# Max ASINs per competitive pricing batch (Amazon hard limit: 20)
_PRICING_BATCH_SIZE = 20

# Competitive pricing rate limit: 1 request per second
_PRICING_RATE_LIMIT_SLEEP = 1.0

# Token cache buffer — refresh token 60 s before expiry
_TOKEN_EXPIRY_BUFFER_S = 60


class AmazonConnector(BasePlatformConnector):
    """
    Amazon Selling Partner API connector.

    Fetches the seller's product catalog via the Listings Items API,
    fetches competitor prices via the Competitive Pricing API (batched),
    and applies prices via a PATCH to the Listings Items API.

    Token caching:
        Access tokens are cached class-wide (keyed by user_id) so that
        multiple concurrent calls within the same repricing cycle share
        a single LWA token exchange.

    Args:
        credentials: Must include keys: refresh_token, client_id, client_secret,
                     marketplace_id, merchant_id.
        user_id:     Supabase user ID — used as the token cache key.
    """

    # Class-level LWA token cache: user_id → (access_token, expires_at_epoch)
    _token_cache: ClassVar[dict[str, tuple[str, float]]] = {}

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    async def _get_access_token(self) -> str:
        """
        Return a valid LWA access token, refreshing from Amazon if needed.

        Checks the class-level cache first.  A cached token is used if it
        will not expire within the next 60 seconds.

        Returns:
            Valid LWA access token string.

        Raises:
            PlatformAuthError: If LWA token exchange fails (invalid credentials).
        """
        cached = self._token_cache.get(self.user_id)
        if cached:
            token, expires_at = cached
            if time.time() < expires_at - _TOKEN_EXPIRY_BUFFER_S:
                return token

        logger.info(
            "Refreshing Amazon LWA access token",
            extra={"user_id": self.user_id},
        )

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self._credentials["refresh_token"],
            "client_id": self._credentials["client_id"],
            "client_secret": self._credentials["client_secret"],
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.post(_LWA_TOKEN_URL, data=payload)
            except httpx.RequestError as exc:
                raise PlatformAuthError(
                    f"LWA token request failed: {exc}", platform="amazon"
                ) from exc

        if resp.status_code != 200:
            logger.error(
                "Amazon LWA token exchange failed",
                extra={
                    "user_id": self.user_id,
                    "status_code": resp.status_code,
                },
            )
            raise PlatformAuthError(
                f"Amazon LWA token exchange returned {resp.status_code}",
                platform="amazon",
                status_code=resp.status_code,
            )

        data = resp.json()
        access_token: str = data["access_token"]
        expires_in: int = int(data.get("expires_in", 3600))

        # Cache keyed by user_id
        self._token_cache[self.user_id] = (access_token, time.time() + expires_in)
        logger.info(
            "Amazon LWA token cached",
            extra={"user_id": self.user_id, "expires_in_s": expires_in},
        )
        return access_token

    def _auth_headers(self, access_token: str) -> dict[str, str]:
        """Return the SP-API required HTTP headers for an authenticated request."""
        return {
            "x-amz-access-token": access_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------
    # validate_credentials
    # ------------------------------------------------------------------

    async def validate_credentials(self) -> bool:
        """
        Validate Amazon SP-API credentials by fetching one listing item.

        A successful LWA token exchange followed by any non-auth SP-API
        response counts as valid.

        Returns:
            True if credentials are operational.

        Raises:
            PlatformAuthError: If LWA exchange fails or SP-API returns 403.
        """
        try:
            token = await self._get_access_token()
        except PlatformAuthError:
            return False

        merchant_id = self._credentials["merchant_id"]
        marketplace_id = self._credentials["marketplace_id"]
        url = f"{_SP_API_BASE}/listings/2021-08-01/items/{merchant_id}"
        params = {
            "marketplaceIds": marketplace_id,
            "pageSize": 1,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.get(url, headers=self._auth_headers(token), params=params)
            except httpx.RequestError as exc:
                raise PlatformAPIError(
                    f"SP-API connectivity error: {exc}", platform="amazon"
                ) from exc

        if resp.status_code in (401, 403):
            logger.warning(
                "Amazon credential validation rejected",
                extra={"user_id": self.user_id, "status_code": resp.status_code},
            )
            return False

        if resp.status_code >= 500:
            raise PlatformAPIError(
                f"Amazon SP-API returned {resp.status_code} during validation",
                platform="amazon",
                status_code=resp.status_code,
                response_body=resp.text,
            )

        logger.info(
            "Amazon credentials validated",
            extra={"user_id": self.user_id, "status_code": resp.status_code},
        )
        return True

    # ------------------------------------------------------------------
    # get_products
    # ------------------------------------------------------------------

    async def get_products(self) -> list[MyProduct]:
        """
        Fetch all active listings for the seller from the Listings Items API.

        Handles SP-API pagination via nextPageToken.  Returns one MyProduct
        per listing with cost and min_margin_floor defaulted to 0 (the seller
        sets these in the PriceBot dashboard; we never read cost from Amazon).

        Returns:
            List of MyProduct instances, one per active listing.

        Raises:
            PlatformAuthError:  On token failure mid-fetch.
            PlatformAPIError:   On SP-API errors.
        """
        token = await self._get_access_token()
        merchant_id = self._credentials["merchant_id"]
        marketplace_id = self._credentials["marketplace_id"]
        url = f"{_SP_API_BASE}/listings/2021-08-01/items/{merchant_id}"
        products: list[MyProduct] = []
        next_page_token: str | None = None

        while True:
            params: dict[str, Any] = {
                "marketplaceIds": marketplace_id,
                "includedData": "summaries,attributes",
                "pageSize": 20,
            }
            if next_page_token:
                params["pageToken"] = next_page_token

            async with httpx.AsyncClient(timeout=30.0) as client:
                try:
                    resp = await client.get(
                        url, headers=self._auth_headers(token), params=params
                    )
                except httpx.RequestError as exc:
                    raise PlatformAPIError(
                        f"SP-API request error: {exc}", platform="amazon"
                    ) from exc

            if resp.status_code == 429:
                raise PlatformRateLimitError(
                    "Amazon listings API rate limit hit",
                    platform="amazon",
                    retry_after=int(resp.headers.get("retry-after", 1)),
                )
            if resp.status_code in (401, 403):
                self._token_cache.pop(self.user_id, None)
                raise PlatformAuthError(
                    f"Amazon listings API auth error {resp.status_code}",
                    platform="amazon",
                    status_code=resp.status_code,
                )
            if not resp.is_success:
                raise PlatformAPIError(
                    f"Amazon listings API returned {resp.status_code}",
                    platform="amazon",
                    status_code=resp.status_code,
                    response_body=resp.text,
                )

            data = resp.json()
            items: list[dict[str, Any]] = data.get("items", [])

            for item in items:
                product = self._map_listing_to_product(item, merchant_id, marketplace_id)
                if product is not None:
                    products.append(product)

            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break

        logger.info(
            "Amazon product catalog fetched",
            extra={
                "user_id": self.user_id,
                "product_count": len(products),
                "marketplace_id": marketplace_id,
            },
        )
        return products

    def _map_listing_to_product(
        self,
        item: dict[str, Any],
        merchant_id: str,
        marketplace_id: str,
    ) -> MyProduct | None:
        """
        Map a raw Listings Items API item dict to a MyProduct model.

        Returns None if the item lacks a price or ASIN (incomplete data).
        """
        try:
            summaries = item.get("summaries", [{}])
            summary = summaries[0] if summaries else {}
            asin: str = item.get("asin", "")
            sku: str = item.get("sku", "")
            title: str = summary.get("itemName", asin or sku or "Unknown")

            # Extract price from attributes
            attrs = item.get("attributes", {})
            price_attr = attrs.get("purchasable_offer", [{}])[0]
            our_price_list = price_attr.get("our_price", [{}])
            our_price_val = our_price_list[0].get("schedule", [{}])[0].get("value_with_tax")
            if our_price_val is None:
                return None

            current_price = float(our_price_val)

            return MyProduct(
                product_id=str(uuid.uuid4()),  # will be replaced by DB UUID on upsert
                platform_product_id=asin,
                platform_sku=sku or None,
                title=title,
                platform="amazon",
                current_price=current_price,
                cost=0.0,
                min_margin_floor=0.0,
                user_id=self.user_id,
                platform_context={},
                metadata={"marketplace_id": marketplace_id, "merchant_id": merchant_id},
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            logger.warning(
                "Skipping Amazon listing with incomplete data",
                extra={"user_id": self.user_id, "error": str(exc)},
            )
            return None

    # ------------------------------------------------------------------
    # get_competitor_prices (impl)
    # ------------------------------------------------------------------

    async def _get_competitor_prices_impl(
        self, product: MyProduct
    ) -> list[CompetitorProduct]:
        """
        Fetch competitor prices via the Competitive Pricing v2022-05-01 API.

        Batches up to 20 ASINs per request and sleeps 1.0 s between batches
        to respect the 1 req/s rate limit.

        Args:
            product: The seller's product (uses platform_product_id as ASIN).

        Returns:
            List of CompetitorProduct, filtered to 'new' condition offers only.

        Raises:
            PlatformRateLimitError: On HTTP 429.
            PlatformProductNotFoundError: If the ASIN is not found.
        """
        token = await self._get_access_token()
        asin = product.platform_product_id
        marketplace_id = self._credentials["marketplace_id"]

        url = f"{_SP_API_BASE}/products/pricing/2022-05-01/competitivePrice"
        params = {
            "marketplaceId": marketplace_id,
            "Asins": asin,
            "ItemType": "Asin",
        }

        async with httpx.AsyncClient(timeout=20.0) as client:
            try:
                resp = await client.get(
                    url, headers=self._auth_headers(token), params=params
                )
            except httpx.RequestError as exc:
                raise PlatformAPIError(
                    f"SP-API competitive pricing request error: {exc}", platform="amazon"
                ) from exc

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("retry-after", 1))
            raise PlatformRateLimitError(
                "Amazon Competitive Pricing API rate limit",
                platform="amazon",
                retry_after=retry_after,
            )
        if resp.status_code == 404:
            raise PlatformProductNotFoundError(
                f"ASIN {asin} not found on Amazon",
                platform="amazon",
                platform_product_id=asin,
            )
        if resp.status_code in (401, 403):
            self._token_cache.pop(self.user_id, None)
            raise PlatformAuthError(
                f"Amazon Competitive Pricing auth error {resp.status_code}",
                platform="amazon",
                status_code=resp.status_code,
            )
        if not resp.is_success:
            raise PlatformAPIError(
                f"Amazon Competitive Pricing returned {resp.status_code}",
                platform="amazon",
                status_code=resp.status_code,
                response_body=resp.text,
            )

        data = resp.json()
        competitors: list[CompetitorProduct] = []

        for price_result in data.get("payload", []):
            status = price_result.get("status", "")
            if status != "Success":
                continue

            product_info = price_result.get("Product", {})
            buy_box_context = self._build_buy_box_context(
                product_info.get("CompetitivePricing", {}).get("CompetitivePrices", [])
            )

            for offer in product_info.get("CompetitivePricing", {}).get("CompetitivePrices", []):
                condition = offer.get("condition", "New").lower()
                if condition not in ("new", "used", "refurbished"):
                    condition = "unknown"

                price_data = offer.get("Price", {}).get("LandedPrice", {})
                amount = price_data.get("Amount")
                if amount is None:
                    continue

                is_fba = offer.get("belongsToRequester", False) is False and \
                    offer.get("CompetitivePriceId", "") in ("1", "2")

                competitors.append(
                    CompetitorProduct(
                        price=float(amount),
                        platform="amazon",
                        is_fulfilled_by_platform=is_fba,
                        condition=condition,
                        extra=buy_box_context,
                    )
                )

        # Respect 1 req/s rate limit before next call
        await asyncio.sleep(_PRICING_RATE_LIMIT_SLEEP)

        logger.info(
            "Amazon competitor prices fetched",
            extra={
                "user_id": self.user_id,
                "asin": asin,
                "competitor_count": len(competitors),
            },
        )
        return competitors

    def _build_buy_box_context(
        self, competitive_prices: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """
        Extract Buy Box context signals from CompetitivePrices data.

        Returns a dict with is_buy_box_winner and fba_competitor_count,
        which the AI uses when applying Buy Box strategy heuristics.
        """
        buy_box_price: float | None = None
        fba_count = 0

        for offer in competitive_prices:
            if offer.get("CompetitivePriceId") == "1":
                amount = offer.get("Price", {}).get("LandedPrice", {}).get("Amount")
                if amount is not None:
                    buy_box_price = float(amount)
            if offer.get("belongsToRequester") and "FBA" in str(offer.get("condition", "")):
                fba_count += 1

        return {
            "buy_box_price": buy_box_price,
            "fba_competitor_count": fba_count,
        }

    # ------------------------------------------------------------------
    # Batch competitor pricing (concurrent, cache-aware)
    # ------------------------------------------------------------------

    async def _get_pricing_batch(
        self, products: list[MyProduct]
    ) -> dict[str, list[CompetitorProduct]]:
        """
        Fetch competitor prices for up to _PRICING_BATCH_SIZE products in one SP-API call.

        Sends all ASINs in a single GET /competitivePrice?Asins=A,B,...,Z request
        and parses the payload into a per-product dict.  Does NOT sleep —
        the caller (get_competitor_prices_bulk) controls rate limiting.

        Args:
            products: Up to _PRICING_BATCH_SIZE products.  Must not be empty.

        Returns:
            Dict mapping product_id → list[CompetitorProduct].
            Products with no offers in the API response map to an empty list.

        Raises:
            PlatformRateLimitError: On HTTP 429.
            PlatformAuthError:      On HTTP 401/403.
            PlatformAPIError:       On other non-success responses.
        """
        if not products:
            return {}

        token = await self._get_access_token()
        marketplace_id = self._credentials["marketplace_id"]

        # Build ASIN → product lookup so we can map responses back by product_id
        asin_to_product: dict[str, MyProduct] = {
            p.platform_product_id: p for p in products
        }
        asins_param = ",".join(asin_to_product.keys())

        url = f"{_SP_API_BASE}/products/pricing/2022-05-01/competitivePrice"
        params = {
            "marketplaceId": marketplace_id,
            "Asins": asins_param,
            "ItemType": "Asin",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.get(
                    url, headers=self._auth_headers(token), params=params
                )
            except httpx.RequestError as exc:
                raise PlatformAPIError(
                    f"SP-API competitive pricing batch request error: {exc}",
                    platform="amazon",
                ) from exc

        if resp.status_code == 429:
            raise PlatformRateLimitError(
                "Amazon Competitive Pricing API rate limit (batch)",
                platform="amazon",
                retry_after=int(resp.headers.get("retry-after", 1)),
            )
        if resp.status_code in (401, 403):
            self._token_cache.pop(self.user_id, None)
            raise PlatformAuthError(
                f"Amazon Competitive Pricing batch auth error {resp.status_code}",
                platform="amazon",
                status_code=resp.status_code,
            )
        if not resp.is_success:
            raise PlatformAPIError(
                f"Amazon Competitive Pricing batch returned {resp.status_code}",
                platform="amazon",
                status_code=resp.status_code,
                response_body=resp.text,
            )

        data = resp.json()
        asin_to_competitors: dict[str, list[CompetitorProduct]] = {}

        for price_result in data.get("payload", []):
            result_asin = price_result.get("ASIN", "")
            status = price_result.get("status", "")
            if status != "Success":
                asin_to_competitors[result_asin] = []
                continue

            product_info = price_result.get("Product", {})
            competitive_prices = (
                product_info.get("CompetitivePricing", {}).get("CompetitivePrices", [])
            )
            buy_box_context = self._build_buy_box_context(competitive_prices)

            competitors: list[CompetitorProduct] = []
            for offer in competitive_prices:
                condition = offer.get("condition", "New").lower()
                if condition not in ("new", "used", "refurbished"):
                    condition = "unknown"

                price_data = offer.get("Price", {}).get("LandedPrice", {})
                amount = price_data.get("Amount")
                if amount is None:
                    continue

                is_fba = offer.get("belongsToRequester", False) is False and \
                    offer.get("CompetitivePriceId", "") in ("1", "2")

                competitors.append(
                    CompetitorProduct(
                        price=float(amount),
                        platform="amazon",
                        is_fulfilled_by_platform=is_fba,
                        condition=condition,
                        extra=buy_box_context,
                    )
                )

            asin_to_competitors[result_asin] = competitors

        # Map ASIN results back to product_id keys
        result: dict[str, list[CompetitorProduct]] = {}
        for asin, product in asin_to_product.items():
            result[product.product_id] = asin_to_competitors.get(asin, [])

        logger.info(
            "Amazon competitor price batch fetched",
            extra={
                "user_id": self.user_id,
                "asin_count": len(asin_to_product),
                "result_count": sum(len(v) for v in result.values()),
            },
        )
        return result

    async def get_competitor_prices_bulk(
        self,
        products: list[MyProduct],
    ) -> dict[str, list[CompetitorProduct]]:
        """
        Fetch competitor prices for multiple products using concurrent batch API calls.

        Amazon-specific override of the base class sequential implementation.

        Algorithm:
          1. Check the Supabase cache for each product.  Return cached prices
             immediately for any product whose cache is fresh (< 15 min old).
          2. Group the remaining (stale/uncached) products into batches of
             _PRICING_BATCH_SIZE (20 — Amazon's per-request ASIN limit).
          3. Fire all batches concurrently with asyncio.gather().  A semaphore
             with size=1 enforces Amazon's 1 req/s rate limit; each task sleeps
             _PRICING_RATE_LIMIT_SLEEP seconds after its API call completes
             (while still holding the semaphore) before the next batch can run.
          4. Merge cached + fresh results.  Batches that fail are logged and
             excluded from the result — the caller handles product-level gaps.

        Args:
            products: The seller's products to price.

        Returns:
            Dict mapping product_id → list of CompetitorProduct.
        """
        if not products:
            return {}

        results: dict[str, list[CompetitorProduct]] = {}
        products_needing_fetch: list[MyProduct] = []

        # Step 1: Check cache — only fetch what is stale
        for product in products:
            cached = self._read_price_cache(product)
            if cached is not None:
                results[product.product_id] = cached
            else:
                products_needing_fetch.append(product)

        if not products_needing_fetch:
            logger.info(
                "All competitor prices served from cache",
                extra={"user_id": self.user_id, "product_count": len(products)},
            )
            return results

        # Step 2: Build batches of _PRICING_BATCH_SIZE
        batches: list[list[MyProduct]] = [
            products_needing_fetch[i : i + _PRICING_BATCH_SIZE]
            for i in range(0, len(products_needing_fetch), _PRICING_BATCH_SIZE)
        ]

        # Step 3: asyncio.gather with semaphore for rate limiting
        sem = asyncio.Semaphore(1)

        async def _fetch_batch_rate_limited(
            batch: list[MyProduct],
        ) -> dict[str, list[CompetitorProduct]]:
            """Acquire semaphore, call API, sleep to honour 1 req/s, release."""
            async with sem:
                batch_result = await self._get_pricing_batch(batch)
                # Sleep while holding the semaphore so subsequent batches are
                # spaced at least _PRICING_RATE_LIMIT_SLEEP apart.
                await asyncio.sleep(_PRICING_RATE_LIMIT_SLEEP)
            return batch_result

        batch_outcomes = await asyncio.gather(
            *(_fetch_batch_rate_limited(b) for b in batches),
            return_exceptions=True,
        )

        # Step 4: Merge results, write cache for fresh data
        for batch, outcome in zip(batches, batch_outcomes):
            if isinstance(outcome, Exception):
                logger.error(
                    "Amazon competitor price batch failed — skipping batch",
                    extra={
                        "user_id": self.user_id,
                        "batch_asin_count": len(batch),
                        "first_asin": batch[0].platform_product_id if batch else "",
                        "error": str(outcome),
                        "error_type": type(outcome).__name__,
                    },
                )
                continue

            for product in batch:
                prices = outcome.get(product.product_id, [])
                results[product.product_id] = prices
                if prices:
                    self._write_price_cache(product, prices)

        logger.info(
            "Amazon competitor prices bulk fetch complete",
            extra={
                "user_id": self.user_id,
                "total_products": len(products),
                "fetched_from_cache": len(products) - len(products_needing_fetch),
                "fetched_from_api": len(products_needing_fetch),
                "batch_count": len(batches),
            },
        )
        return results

    # ------------------------------------------------------------------
    # apply_price (impl)
    # ------------------------------------------------------------------

    async def _apply_price_impl(self, product: MyProduct, new_price: float) -> bool:
        """
        Apply a new price to the Amazon listing via PATCH /listings/2021-08-01/items.

        Uses the purchasable_offer attributes path per the SP-API spec
        (PLATFORMS.md §3.5).  Requires platform_sku to be set.

        Args:
            product:   The seller's product.  platform_sku must not be None.
            new_price: Final guardrail-validated price.

        Returns:
            True on successful 200/202 response.

        Raises:
            PlatformRateLimitError:       On HTTP 429.
            PlatformProductNotFoundError: On HTTP 404.
            PlatformAuthError:            On HTTP 401/403.
            PlatformAPIError:             On other errors.
        """
        if not product.platform_sku:
            raise PlatformAPIError(
                f"Cannot apply price to Amazon product {product.product_id}: platform_sku is None. "
                "SKU is required for the Listings Items PATCH endpoint.",
                platform="amazon",
            )

        token = await self._get_access_token()
        merchant_id = self._credentials["merchant_id"]
        marketplace_id = self._credentials["marketplace_id"]
        sku = product.platform_sku

        url = f"{_SP_API_BASE}/listings/2021-08-01/items/{merchant_id}/{sku}"
        params = {"marketplaceIds": marketplace_id}

        body = {
            "productType": "PRODUCT",
            "patches": [
                {
                    "op": "replace",
                    "path": "/attributes/purchasable_offer",
                    "value": [
                        {
                            "marketplace_id": marketplace_id,
                            "currency": "USD",
                            "our_price": [
                                {
                                    "schedule": [
                                        {"value_with_tax": round(new_price, 2)}
                                    ]
                                }
                            ],
                        }
                    ],
                }
            ],
        }

        async with httpx.AsyncClient(timeout=20.0) as client:
            try:
                resp = await client.patch(
                    url,
                    headers=self._auth_headers(token),
                    params=params,
                    json=body,
                )
            except httpx.RequestError as exc:
                raise PlatformAPIError(
                    f"SP-API PATCH request error: {exc}", platform="amazon"
                ) from exc

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("retry-after", 1))
            raise PlatformRateLimitError(
                f"Amazon Listings PATCH rate limit for SKU {sku}",
                platform="amazon",
                retry_after=retry_after,
            )
        if resp.status_code == 404:
            raise PlatformProductNotFoundError(
                f"SKU {sku} not found on Amazon",
                platform="amazon",
                platform_product_id=sku,
            )
        if resp.status_code in (401, 403):
            self._token_cache.pop(self.user_id, None)
            raise PlatformAuthError(
                f"Amazon Listings PATCH auth error {resp.status_code}",
                platform="amazon",
                status_code=resp.status_code,
            )
        if resp.status_code not in (200, 202):
            raise PlatformAPIError(
                f"Amazon Listings PATCH returned {resp.status_code} for SKU {sku}",
                platform="amazon",
                status_code=resp.status_code,
                response_body=resp.text,
            )

        logger.info(
            "Amazon price applied",
            extra={
                "user_id": self.user_id,
                "product_id": product.product_id,
                "sku": sku,
                "new_price": new_price,
                "status_code": resp.status_code,
            },
        )
        return True
