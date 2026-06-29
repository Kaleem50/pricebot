"""
platforms/exceptions.py — Platform Connector Exception Hierarchy

All exceptions raised by platform connectors inherit from PlatformError.
This lets callers catch the full hierarchy with a single except clause
or discriminate by subtype for retry/escalation decisions.

Usage in connectors::

    raise PlatformRateLimitError(
        "Amazon SP-API returned 429",
        platform="amazon",
        status_code=429,
    )

Usage in callers::

    try:
        prices = await connector.get_competitor_prices(product)
    except PlatformRateLimitError:
        # tenacity retries — this branch is hit after all retries exhausted
        logger.error("Rate limit unrecoverable after 3 retries")
    except PlatformAuthError:
        # Deactivate the platform connection
        ...
    except PlatformError:
        # Catch-all for any other connector failure
        ...
"""

from __future__ import annotations


class PlatformError(Exception):
    """
    Base exception for all platform connector errors.

    Args:
        message:     Human-readable description of the error.
        platform:    Platform identifier ('amazon', 'etsy', etc.).
        status_code: HTTP status code from the platform API, if applicable.
    """

    def __init__(
        self,
        message: str,
        platform: str = "",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.platform = platform
        self.status_code = status_code


class PlatformAuthError(PlatformError):
    """
    Raised when platform credentials are invalid or the access token has expired
    and cannot be refreshed.

    Callers should deactivate the platform_connection row in the DB and surface
    a re-authentication prompt to the user.
    """


class PlatformRateLimitError(PlatformError):
    """
    Raised when the platform API returns HTTP 429 (Too Many Requests).

    The tenacity retry decorator on get_competitor_prices() and apply_price()
    catches this exception and backs off exponentially (2–10 s, max 3 attempts).
    If all retries are exhausted tenacity re-raises this exception to the caller.

    Args:
        retry_after: Seconds to wait before retrying, from the Retry-After header
                     if provided by the platform.
    """

    def __init__(
        self,
        message: str,
        platform: str = "",
        status_code: int = 429,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message, platform=platform, status_code=status_code)
        self.retry_after = retry_after


class PlatformProductNotFoundError(PlatformError):
    """
    Raised when the platform cannot find a product by the given identifier.

    This is a non-retryable error — the product may have been deleted from
    the platform.  Callers should mark the product as inactive in the DB.

    Args:
        platform_product_id: The ASIN, listing ID, or other native identifier
                             that was not found.
    """

    def __init__(
        self,
        message: str,
        platform: str = "",
        platform_product_id: str = "",
    ) -> None:
        super().__init__(message, platform=platform, status_code=404)
        self.platform_product_id = platform_product_id


class PlatformAPIError(PlatformError):
    """
    Raised for unexpected platform API errors not covered by the above subtypes.

    Includes 5xx server errors, malformed responses, and protocol-level failures.
    The raw response body is optionally attached for debugging.

    Args:
        response_body: Raw response text from the platform API (truncated to 500 chars).
    """

    def __init__(
        self,
        message: str,
        platform: str = "",
        status_code: int | None = None,
        response_body: str = "",
    ) -> None:
        super().__init__(message, platform=platform, status_code=status_code)
        self.response_body = response_body[:500] if response_body else ""
