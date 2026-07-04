"""
platforms/etsy.py — Etsy API Connector

Implements BasePlatformConnector for the Etsy Open API v3.

Authentication (PLATFORMS.md §4.1):
  Etsy uses OAuth 2.0 with PKCE. The connector stores access_token (1 hour),
  refresh_token (90 days), and shop_id in the encrypted credentials dict.
  Access tokens are refreshed automatically on 401 responses.

Required credentials keys:
  - access_token:   Short-lived OAuth 2.0 bearer token (1 hour)
  - refresh_token:  Long-lived refresh token (90 days)
  - shop_id:        Seller's numeric Etsy shop ID (string)

Environment variables:
  - ETSY_CLIENT_ID:     OAuth 2.0 app client ID (Keystring)
  - ETSY_CLIENT_SECRET: OAuth 2.0 app client secret

Rate limits (PLATFORMS.md §4.4):
  - 10,000 requests/day per app (shared across all PriceBot users)
  - Daily usage is tracked in the usage_events table
  - PlatformRateLimitError raised if daily count exceeds 9,500

Competitor price discovery:
  Etsy has no ASIN-style lookup. Keyword search on product title words
  is used instead. Results are marked approximate in platform_context so
  the AI can reason accordingly (search_method=keyword).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import httpx

from core.repricing_engine import CompetitorProduct, MyProduct
from platforms.base import BasePlatformConnector
from platforms.exceptions import (
    PlatformAPIError,
    PlatformAuthError,
    PlatformRateLimitError,
)

logger = logging.getLogger(__name__)

_ETSY_BASE = "https://openapi.etsy.com/v3/application"
_OAUTH_TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"

# Daily request budget — Etsy allows 10,000/day per app across all users.
# Stop at 9,500 to leave headroom for validation calls.
_DAILY_RATE_LIMIT = 9_500

# Max competitor results to return from keyword search
_MAX_COMPETITOR_RESULTS = 10


class EtsyConnector(BasePlatformConnector):
    """
    Etsy Open API v3 connector.

    Fetches the seller's active listings, searches for competitors by
    keyword, and applies price updates via PATCH.  Access tokens are
    refreshed transparently on 401 responses.

    Args:
        credentials: Must include keys: access_token, refresh_token, shop_id.
        user_id:     Supabase user ID of the credential owner.
        db:          Optional Supabase Client for competitor price caching.
    """

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def __init__(
        self,
        credentials: dict[str, str],
        user_id: str,
        db: object | None = None,
    ) -> None:
        """
        Initialise the Etsy connector.

        Args:
            credentials: Decrypted credential dict with access_token,
                         refresh_token, and shop_id.
            user_id:     Supabase user ID.
            db:          Optional Supabase Client for caching.

        Raises:
            ValueError: If required credential keys are missing.
        """
        super().__init__(credentials=credentials, user_id=user_id, db=db)

        for required in ("access_token", "refresh_token", "shop_id"):
            if required not in credentials:
                raise ValueError(
                    f"Etsy credentials missing required key: {required!r}"
                )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_headers(self) -> dict[str, str]:
        """Build Etsy API request headers with current access token."""
        client_id = os.environ.get("ETSY_CLIENT_ID", "")
        return {
            "x-api-key": client_id,
            "Authorization": f"Bearer {self._credentials['access_token']}",
            "Content-Type": "application/json",
        }

    async def _refresh_access_token(self) -> None:
        """
        Exchange the refresh token for a new access token.

        Updates self._credentials["access_token"] in memory only — the
        caller is responsible for persisting the new token to the DB if
        needed.  The token value is never logged at any level.

        Raises:
            PlatformAuthError: On 400/401 from the Etsy token endpoint
                               (refresh token expired or revoked).
            PlatformAPIError:  On unexpected non-2xx responses.
        """
        client_id = os.environ.get("ETSY_CLIENT_ID", "")
        client_secret = os.environ.get("ETSY_CLIENT_SECRET", "")

        async with httpx.AsyncClient(timeout=30.0) as http:
            response = await http.post(
                _OAUTH_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": self._credentials["refresh_token"],
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if response.status_code in (400, 401):
            logger.warning(
                "Etsy token refresh failed — refresh token invalid or revoked",
                extra={"user_id": self.user_id, "status_code": response.status_code},
            )
            raise PlatformAuthError(
                "Etsy refresh token is invalid or expired — please reconnect your account.",
                platform="etsy",
                status_code=response.status_code,
            )

        if not response.is_success:
            raise PlatformAPIError(
                f"Etsy token refresh returned unexpected status {response.status_code}",
                platform="etsy",
                status_code=response.status_code,
                response_body=response.text,
            )

        data = response.json()
        # Update in memory — never log the token value
        self._credentials["access_token"] = data["access_token"]
        logger.info(
            "Etsy access token refreshed successfully",
            extra={"user_id": self.user_id},
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        _is_retry: bool = False,
    ) -> httpx.Response:
        """
        Perform an authenticated HTTP request with automatic token refresh.

        On first 401: refresh the access token and retry once.
        On second 401: raise PlatformAuthError (refresh token also expired).
        On 429: raise PlatformRateLimitError (tenacity handles retry).

        Args:
            method:    HTTP method ('GET', 'PATCH', etc.).
            url:       Full Etsy API URL.
            params:    Query parameters dict.
            json:      Request body dict (for PATCH/POST).
            _is_retry: True if this is the post-refresh retry attempt.

        Returns:
            httpx.Response on success (2xx).

        Raises:
            PlatformAuthError:      On 401 after refresh, or 403.
            PlatformRateLimitError: On 429.
            PlatformAPIError:       On other non-2xx responses.
        """
        await self._check_daily_rate_limit()

        async with httpx.AsyncClient(timeout=30.0) as http:
            response = await http.request(
                method,
                url,
                params=params,
                json=json,
                headers=self._get_headers(),
            )

        self._record_api_call()

        if response.status_code == 401:
            if _is_retry:
                raise PlatformAuthError(
                    "Etsy access token refresh did not restore authentication — "
                    "refresh token may be expired.",
                    platform="etsy",
                    status_code=401,
                )
            logger.info(
                "Etsy 401 received — refreshing access token and retrying",
                extra={"user_id": self.user_id},
            )
            await self._refresh_access_token()
            return await self._request(
                method, url, params=params, json=json, _is_retry=True
            )

        if response.status_code == 403:
            raise PlatformAuthError(
                "Etsy returned 403 — check OAuth scopes.",
                platform="etsy",
                status_code=403,
            )

        if response.status_code == 429:
            raise PlatformRateLimitError(
                "Etsy rate limit hit (429)",
                platform="etsy",
                status_code=429,
            )

        return response

    # ------------------------------------------------------------------
    # Daily rate limit tracking
    # ------------------------------------------------------------------

    def _record_api_call(self) -> None:
        """
        Record one API call in the usage_events table.

        Silently skips on any DB error — a tracking failure must not
        block the repricing pipeline.
        """
        if self._db is None:
            return
        try:
            self._db.table("usage_events").insert(
                {
                    "user_id": self.user_id,
                    "platform": "etsy",
                    "event_type": "api_call",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ).execute()
        except Exception as exc:
            logger.warning(
                "Failed to record Etsy API usage event",
                extra={"user_id": self.user_id, "error": str(exc)},
            )

    async def _check_daily_rate_limit(self) -> None:
        """
        Check the daily Etsy API call count across all PriceBot users.

        Etsy's 10,000/day limit is per app, shared across all users.
        Raises PlatformRateLimitError if the global count exceeds 9,500.

        Silently passes if usage_events table is unavailable.

        Raises:
            PlatformRateLimitError: If daily app-level usage > 9,500.
        """
        if self._db is None:
            return
        try:
            today = datetime.now(timezone.utc).date().isoformat()
            result = (
                self._db.table("usage_events")
                .select("id", count="exact")
                .eq("platform", "etsy")
                .eq("event_type", "api_call")
                .gte("created_at", f"{today}T00:00:00+00:00")
                .execute()
            )
            daily_count = result.count or 0
            if daily_count >= _DAILY_RATE_LIMIT:
                logger.warning(
                    "Etsy daily API rate limit reached",
                    extra={
                        "user_id": self.user_id,
                        "daily_count": daily_count,
                        "limit": _DAILY_RATE_LIMIT,
                    },
                )
                raise PlatformRateLimitError(
                    f"Etsy daily API limit reached ({daily_count}/{_DAILY_RATE_LIMIT}). "
                    "Repricing will resume tomorrow.",
                    platform="etsy",
                    status_code=429,
                )
        except PlatformRateLimitError:
            raise
        except Exception as exc:
            logger.warning(
                "Failed to check Etsy daily rate limit — proceeding",
                extra={"user_id": self.user_id, "error": str(exc)},
            )

    # ------------------------------------------------------------------
    # BasePlatformConnector implementation
    # ------------------------------------------------------------------

    async def validate_credentials(self) -> bool:
        """
        Verify Etsy credentials by calling the /users/me endpoint.

        Returns:
            True on 200.

        Raises:
            PlatformAuthError: On 401 or 403.
            PlatformAPIError:  On unexpected non-2xx responses.
        """
        url = f"{_ETSY_BASE}/users/me"
        try:
            response = await self._request("GET", url)
        except (PlatformAuthError, PlatformAPIError):
            raise

        if response.is_success:
            logger.info(
                "Etsy credentials validated successfully",
                extra={"user_id": self.user_id},
            )
            return True

        raise PlatformAPIError(
            f"Etsy credential validation returned {response.status_code}",
            platform="etsy",
            status_code=response.status_code,
            response_body=response.text,
        )

    async def get_products(self) -> list[MyProduct]:
        """
        Fetch all active listings for the seller's Etsy shop.

        Paginates with limit=100 until the results array is empty.
        Price is converted from Etsy's integer format (amount/divisor).

        Returns:
            List of MyProduct instances for active listings only.

        Raises:
            PlatformAuthError:      On credential failure mid-fetch.
            PlatformAPIError:       On unexpected platform errors.
            PlatformRateLimitError: If daily rate limit exceeded.
        """
        shop_id = self._credentials["shop_id"]
        url = f"{_ETSY_BASE}/shops/{shop_id}/listings/active"
        all_products: list[MyProduct] = []
        offset = 0
        limit = 100

        while True:
            response = await self._request(
                "GET",
                url,
                params={"limit": limit, "offset": offset},
            )

            if not response.is_success:
                raise PlatformAPIError(
                    f"Etsy listings fetch returned {response.status_code}",
                    platform="etsy",
                    status_code=response.status_code,
                    response_body=response.text,
                )

            data = response.json()
            results = data.get("results", [])

            if not results:
                break

            for listing in results:
                if listing.get("state") != "active":
                    continue

                price_obj = listing.get("price", {})
                amount = price_obj.get("amount", 0)
                divisor = price_obj.get("divisor", 100)
                current_price = round(amount / divisor, 2) if divisor else 0.0

                if current_price <= 0:
                    continue

                all_products.append(
                    MyProduct(
                        product_id=str(listing["listing_id"]),
                        platform_product_id=str(listing["listing_id"]),
                        platform_sku=None,
                        title=listing.get("title", ""),
                        current_price=current_price,
                        platform="etsy",
                        user_id=self.user_id,
                    )
                )

            if len(results) < limit:
                break

            offset += limit

        logger.info(
            "Etsy product catalog fetched",
            extra={
                "user_id": self.user_id,
                "shop_id": shop_id,
                "product_count": len(all_products),
            },
        )
        return all_products

    async def _get_competitor_prices_impl(
        self, product: MyProduct
    ) -> list[CompetitorProduct]:
        """
        Search Etsy for competitor listings using the first 5 words of the title.

        Results are filtered to exclude the seller's own listings and capped
        at the top 10 cheapest. platform_context["search_method"] = "keyword"
        is set on the product so Claude knows this data is approximate.

        Args:
            product: The seller's product to find competitors for.

        Returns:
            List of up to 10 CompetitorProduct instances sorted by price asc.

        Raises:
            PlatformRateLimitError: On 429 — triggers tenacity retry.
            PlatformAuthError:      On credential failure.
            PlatformAPIError:       On other unexpected errors.
        """
        shop_id = self._credentials["shop_id"]
        keywords = " ".join(product.title.split()[:5])
        url = f"{_ETSY_BASE}/listings/active"

        response = await self._request(
            "GET",
            url,
            params={
                "keywords": keywords,
                "limit": 20,
                "sort_on": "price",
                "sort_order": "ascending",
            },
        )

        if not response.is_success:
            raise PlatformAPIError(
                f"Etsy competitor search returned {response.status_code}",
                platform="etsy",
                status_code=response.status_code,
                response_body=response.text,
            )

        data = response.json()
        results = data.get("results", [])
        competitors: list[CompetitorProduct] = []

        for listing in results:
            # Filter out seller's own listings by shop_id in the listing URL
            listing_url = listing.get("url", "")
            if f"/shop/{shop_id}/" in listing_url:
                continue

            price_obj = listing.get("price", {})
            amount = price_obj.get("amount", 0)
            divisor = price_obj.get("divisor", 100)
            price = round(amount / divisor, 2) if divisor else 0.0

            if price <= 0:
                continue

            competitors.append(
                CompetitorProduct(
                    price=price,
                    platform="etsy",
                    is_fulfilled_by_platform=False,
                    condition="new",
                    extra={
                        "search_method": "keyword",
                        "match_type": "approximate",
                    },
                )
            )

            if len(competitors) >= _MAX_COMPETITOR_RESULTS:
                break

        logger.info(
            "Etsy competitor prices fetched via keyword search",
            extra={
                "user_id": self.user_id,
                "product_id": product.product_id,
                "keywords": keywords,
                "competitor_count": len(competitors),
            },
        )

        return competitors

    async def _apply_price_impl(
        self, product: MyProduct, new_price: float
    ) -> bool:
        """
        Update the listing price on Etsy via PATCH.

        Args:
            product:   The seller's product with platform_product_id set.
            new_price: Guardrail-validated final price.

        Returns:
            True on success (200 or 204).

        Raises:
            PlatformRateLimitError: On 429.
            PlatformAuthError:      On 401/403.
            PlatformAPIError:       On any other non-2xx response.
        """
        shop_id = self._credentials["shop_id"]
        listing_id = product.platform_product_id
        url = f"{_ETSY_BASE}/shops/{shop_id}/listings/{listing_id}"

        response = await self._request(
            "PATCH",
            url,
            json={"price": round(new_price, 2)},
        )

        if response.status_code in (200, 204):
            logger.info(
                "Etsy price updated",
                extra={
                    "user_id": self.user_id,
                    "listing_id": listing_id,
                    "new_price": new_price,
                },
            )
            return True

        raise PlatformAPIError(
            f"Etsy price update failed with status {response.status_code}",
            platform="etsy",
            status_code=response.status_code,
            response_body=response.text,
        )
