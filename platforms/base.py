"""
platforms/base.py — Abstract Platform Connector Base Class

Defines the interface all platform connectors must implement.

Caching (002_competitor_price_cache.sql):
  get_competitor_prices() checks the products table for a fresh cache entry.
  If competitor_prices_cached_at is within 15 minutes, returns the cached
  JSON without hitting the platform API.  Pass a Supabase Client via the
  ``db`` constructor parameter to enable caching; omit it to disable.

Retry behaviour (ARCHITECTURE.md §4.2):
  _fetch_competitor_prices_with_retry() and _apply_price_with_retry() wrap
  the abstract _impl methods with tenacity so PlatformRateLimitError triggers
  automatic exponential back-off (2–10 s, max 3 attempts).

Security constraints (CLAUDE.md §5.3):
  - Credentials must never be logged, persisted, or returned in responses.
  - user_id is stamped onto every MyProduct for per-user isolation.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any

from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.repricing_engine import CompetitorProduct, MyProduct
from platforms.exceptions import PlatformRateLimitError

logger = logging.getLogger(__name__)

_CACHE_TTL_MINUTES = 15


class BasePlatformConnector(ABC):
    """
    Abstract base class for all PriceBot platform connectors.

    Subclasses must implement:
      - validate_credentials()        — lightweight credential check
      - get_products()                — fetch the seller's full product catalog
      - _get_competitor_prices_impl() — raw platform API call (one product)
      - _apply_price_impl()           — raw platform API call (write price)

    Public interface:
      - get_competitor_prices(product)        — cache-aware fetch (one product)
      - get_competitor_prices_bulk(products)  — concurrent batch fetch
      - apply_price(product, new_price)       — rate-limit retry wrapper

    Args:
        credentials: Decrypted credential dict.  Never log or store.
        user_id:     Supabase auth.users.id — stamped on returned MyProducts.
        db:          Optional Supabase Client.  When provided, competitor price
                     results are cached in the products table and returned from
                     cache if fetched_at < 15 minutes ago.
    """

    def __init__(
        self,
        credentials: dict[str, str],
        user_id: str,
        db: Any | None = None,
    ) -> None:
        """
        Initialise the connector.

        Args:
            credentials: Decrypted platform credentials dict.
            user_id:     Supabase user ID of the credential owner.
            db:          Optional Supabase Client for competitor price caching.
        """
        if not credentials:
            raise ValueError("credentials dict must not be empty")
        if not user_id:
            raise ValueError("user_id must not be empty")
        self._credentials = credentials
        self.user_id = user_id
        self._db = db  # None = caching disabled

    # ------------------------------------------------------------------
    # Abstract interface — every connector must implement these
    # ------------------------------------------------------------------

    @abstractmethod
    async def validate_credentials(self) -> bool:
        """
        Verify credentials are valid via a lightweight API call.

        Returns:
            True if credentials are operational; False if invalid.

        Raises:
            PlatformAuthError:  On authentication failure.
            PlatformAPIError:   On unexpected API errors.
        """

    @abstractmethod
    async def get_products(self) -> list[MyProduct]:
        """
        Fetch the seller's complete product catalog from the platform.

        Handles pagination internally.  Returns all products in one call.

        Returns:
            List of MyProduct instances.

        Raises:
            PlatformAuthError:  On credential failure mid-fetch.
            PlatformAPIError:   On unexpected platform errors.
        """

    @abstractmethod
    async def _get_competitor_prices_impl(
        self, product: MyProduct
    ) -> list[CompetitorProduct]:
        """
        Raw platform API call to fetch competitor prices for one product.

        Must raise PlatformRateLimitError on HTTP 429.
        Called by the retry wrapper — do not call directly.

        Raises:
            PlatformRateLimitError:         On HTTP 429 — triggers tenacity retry.
            PlatformProductNotFoundError:   On HTTP 404.
            PlatformAuthError:              On credential failure.
            PlatformAPIError:               On other platform errors.
        """

    @abstractmethod
    async def _apply_price_impl(
        self, product: MyProduct, new_price: float
    ) -> bool:
        """
        Raw platform API call to write a new price.

        Must raise PlatformRateLimitError on HTTP 429.
        Called by the retry wrapper — do not call directly.

        Raises:
            PlatformRateLimitError:         On HTTP 429 — triggers tenacity retry.
            PlatformProductNotFoundError:   On HTTP 404.
            PlatformAuthError:              On credential failure.
            PlatformAPIError:               On other platform errors.
        """

    # ------------------------------------------------------------------
    # Cache helpers (no-ops when self._db is None)
    # ------------------------------------------------------------------

    def _read_price_cache(
        self, product: MyProduct
    ) -> list[CompetitorProduct] | None:
        """
        Return cached competitor prices if the cache entry is fresh.

        Queries the products table for competitor_prices_cached_at and
        competitor_prices_cache.  Returns None if the cache is stale
        (>15 min old) or not yet populated.

        Args:
            product: The product to look up in the cache.

        Returns:
            Parsed list of CompetitorProduct if cache is fresh; None if stale.
        """
        if self._db is None:
            return None
        try:
            result = (
                self._db.table("products")
                .select("competitor_prices_cached_at, competitor_prices_cache")
                .eq("id", product.product_id)
                .eq("user_id", self.user_id)
                .execute()
            )
            if not result.data:
                return None
            row = result.data[0]
            cached_at_raw = row.get("competitor_prices_cached_at")
            if not cached_at_raw:
                return None

            # Parse timestamp (Supabase returns ISO 8601 string)
            cached_at = datetime.fromisoformat(
                cached_at_raw.replace("Z", "+00:00")
            )
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=_CACHE_TTL_MINUTES)
            if cached_at < cutoff:
                return None  # Cache is stale

            cache_json = row.get("competitor_prices_cache") or []
            if not cache_json:
                return None

            competitors = [
                CompetitorProduct(**entry)
                for entry in cache_json
            ]
            logger.info(
                "Competitor price cache hit",
                extra={
                    "product_id": product.product_id,
                    "user_id": self.user_id,
                    "cached_count": len(competitors),
                    "cached_at": cached_at_raw,
                },
            )
            return competitors
        except Exception as exc:
            logger.warning(
                "Failed to read competitor price cache",
                extra={
                    "product_id": product.product_id,
                    "user_id": self.user_id,
                    "error": str(exc),
                },
            )
            return None  # Cache miss on error — proceed with live fetch

    def _write_price_cache(
        self, product: MyProduct, prices: list[CompetitorProduct]
    ) -> None:
        """
        Persist fresh competitor prices to the products table cache columns.

        Silently skips on any DB error — a cache write failure must never
        block the repricing pipeline.

        Args:
            product: The product whose cache entry to update.
            prices:  Fresh competitor prices from the platform API.
        """
        if self._db is None:
            return
        try:
            cache_payload = [p.model_dump() for p in prices]
            self._db.table("products").update(
                {
                    "competitor_prices_cached_at": datetime.now(timezone.utc).isoformat(),
                    "competitor_prices_cache": cache_payload,
                }
            ).eq("id", product.product_id).eq("user_id", self.user_id).execute()
        except Exception as exc:
            logger.warning(
                "Failed to write competitor price cache — continuing",
                extra={
                    "product_id": product.product_id,
                    "user_id": self.user_id,
                    "error": str(exc),
                },
            )

    # ------------------------------------------------------------------
    # Retry wrappers (private)
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(PlatformRateLimitError),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _fetch_competitor_prices_with_retry(
        self, product: MyProduct
    ) -> list[CompetitorProduct]:
        """Retry wrapper for _get_competitor_prices_impl."""
        return await self._get_competitor_prices_impl(product)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(PlatformRateLimitError),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _apply_price_with_retry(
        self, product: MyProduct, new_price: float
    ) -> bool:
        """Retry wrapper for _apply_price_impl."""
        return await self._apply_price_impl(product, new_price)

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    async def get_competitor_prices(
        self, product: MyProduct
    ) -> list[CompetitorProduct]:
        """
        Fetch live competitor prices with cache check and retry on rate limits.

        Cache flow (when db= was provided to __init__):
          1. Query products.competitor_prices_cached_at for this product.
          2. If cached_at is within 15 minutes, return cached prices immediately.
          3. Otherwise fetch from platform API via _fetch_competitor_prices_with_retry().
          4. Write the fresh result to cache.

        On PlatformRateLimitError, tenacity backs off exponentially (2–10 s)
        and retries up to 3 times before re-raising.

        Args:
            product: The seller's product to find competitors for.

        Returns:
            List of CompetitorProduct instances.

        Raises:
            PlatformRateLimitError:       After 3 retries exhausted.
            PlatformProductNotFoundError: Immediately (not retried).
            PlatformAuthError:            Immediately (not retried).
            PlatformAPIError:             Immediately (not retried).
        """
        # Check cache first
        cached = self._read_price_cache(product)
        if cached is not None:
            return cached

        # Fetch from platform (with retry)
        prices = await self._fetch_competitor_prices_with_retry(product)

        # Persist to cache for next cycle
        if prices is not None:
            self._write_price_cache(product, prices)

        return prices

    async def get_competitor_prices_bulk(
        self,
        products: list[MyProduct],
    ) -> dict[str, list[CompetitorProduct]]:
        """
        Fetch competitor prices for multiple products concurrently.

        Base implementation calls get_competitor_prices() sequentially.
        Subclasses may override to use platform-specific batch endpoints
        with asyncio.gather() for concurrent execution.

        Args:
            products: List of the seller's products to price.

        Returns:
            Dict mapping product_id → list of CompetitorProduct.
            Products that error are omitted (caller must handle gaps).
        """
        results: dict[str, list[CompetitorProduct]] = {}
        for product in products:
            try:
                prices = await self.get_competitor_prices(product)
                results[product.product_id] = prices
            except Exception as exc:
                logger.error(
                    "get_competitor_prices_bulk: error for product",
                    extra={
                        "product_id": product.product_id,
                        "user_id": self.user_id,
                        "error": str(exc),
                    },
                )
        return results

    async def apply_price(self, product: MyProduct, new_price: float) -> bool:
        """
        Write a new price to the platform with automatic retry on rate limits.

        The caller must have applied the fail-safe guardrail before calling.

        Args:
            product:   The seller's product.
            new_price: The guardrail-validated final price to write.

        Returns:
            True on success.

        Raises:
            PlatformRateLimitError:       After 3 retries exhausted.
            PlatformProductNotFoundError: Immediately (not retried).
            PlatformAuthError:            Immediately (not retried).
            PlatformAPIError:             Immediately (not retried).
        """
        return await self._apply_price_with_retry(product, new_price)
