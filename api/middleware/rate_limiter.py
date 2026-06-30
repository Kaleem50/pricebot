"""
api/middleware/rate_limiter.py — Token-Bucket Rate Limiter

Implements per-user, per-tier token-bucket rate limiting as defined in
SECURITY.md §5.1.  Two layers are provided:

  1. ``RateLimiterMiddleware`` — Starlette ASGI middleware that applies a
     blanket request-per-minute limit.  Extracts ``user_id`` from the JWT
     payload without full signature verification (verification happens in
     ``auth_guard.py``).  Falls back to client IP for unauthenticated requests.

  2. ``rate_limit`` — FastAPI dependency that applies tier-specific limits
     after full authentication.  Use this on routes that need precise per-tier
     enforcement.

Rate limits (SECURITY.md §5.1):
  - Starter: 30 requests/min
  - Growth:  60 requests/min
  - Pro:     120 requests/min

State is in-memory; the process must be single-worker for these limits to be
perfectly accurate.  For multi-worker deployments, replace ``_buckets`` with
a Redis-backed store.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

import jwt as pyjwt
from fastapi import Depends, HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from api.dependencies import AuthenticatedUser, Tier, get_current_user

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class RateLimit:
    """Configuration for a single tier's rate limit."""

    requests_per_minute: int
    batch_submits_per_hour: int


RATE_LIMITS: dict[Tier, RateLimit] = {
    Tier.STARTER: RateLimit(requests_per_minute=30, batch_submits_per_hour=5),
    Tier.GROWTH:  RateLimit(requests_per_minute=60, batch_submits_per_hour=10),
    Tier.PRO:     RateLimit(requests_per_minute=120, batch_submits_per_hour=20),
}

#: Default limit applied to unauthenticated requests and as a baseline in the
#: middleware before tier is known (uses Growth as a conservative middle ground).
_DEFAULT_REQUESTS_PER_MINUTE = 60


class TokenBucket:
    """
    Thread-safe token bucket for rate limiting.

    Tokens are added continuously at ``refill_rate`` tokens per second up to
    ``capacity``.  Each request consumes one token.  When the bucket is empty,
    ``consume()`` returns ``(False, retry_after_seconds)``.
    """

    def __init__(self, capacity: int, refill_rate: float) -> None:
        """
        Initialise the bucket fully loaded.

        Args:
            capacity:    Maximum number of tokens (burst size).
            refill_rate: Tokens added per second.
        """
        self._capacity = capacity
        self._refill_rate = refill_rate
        self._tokens: float = float(capacity)
        self._last_refill: float = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, tokens: int = 1) -> tuple[bool, float]:
        """
        Attempt to consume ``tokens`` from the bucket.

        Args:
            tokens: Number of tokens to consume (typically 1 per request).

        Returns:
            ``(True, 0.0)`` if tokens were available and consumed.
            ``(False, retry_after)`` if the bucket was empty, where
            ``retry_after`` is the number of seconds to wait.
        """
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(
                self._capacity,
                self._tokens + elapsed * self._refill_rate,
            )
            self._last_refill = now

            if self._tokens >= tokens:
                self._tokens -= tokens
                return True, 0.0

            deficit = tokens - self._tokens
            retry_after = deficit / self._refill_rate
            return False, retry_after

    @property
    def available(self) -> float:
        """Current token count (approximate — not locked)."""
        return self._tokens


class RateLimiter:
    """
    Manages a pool of ``TokenBucket`` instances keyed by a string identifier
    (typically ``user_id`` or client IP).

    Buckets are created lazily on first access.  This is an in-memory store;
    in a multi-worker deployment it should be replaced with a Redis backend.
    """

    def __init__(
        self,
        default_capacity: int = _DEFAULT_REQUESTS_PER_MINUTE,
        default_refill_rate: Optional[float] = None,
    ) -> None:
        """
        Args:
            default_capacity:    Default bucket size (burst limit).
            default_refill_rate: Tokens/second.  Defaults to capacity / 60
                                 (i.e. one full minute to refill from empty).
        """
        self._default_capacity = default_capacity
        self._default_refill_rate = default_refill_rate or (default_capacity / 60.0)
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def _get_or_create(
        self,
        key: str,
        capacity: Optional[int] = None,
        refill_rate: Optional[float] = None,
    ) -> TokenBucket:
        """Return existing bucket or create a new one for ``key``."""
        with self._lock:
            if key not in self._buckets:
                cap = capacity or self._default_capacity
                rate = refill_rate or (cap / 60.0)
                self._buckets[key] = TokenBucket(capacity=cap, refill_rate=rate)
            return self._buckets[key]

    def check(
        self,
        key: str,
        tier: Optional[Tier] = None,
    ) -> tuple[bool, float]:
        """
        Check and consume one token for ``key``.

        Args:
            key:  Bucket identifier (user_id or IP).
            tier: If provided, applies the tier-specific capacity.

        Returns:
            ``(allowed, retry_after_seconds)`` from ``TokenBucket.consume()``.
        """
        if tier is not None:
            limit = RATE_LIMITS[tier]
            capacity = limit.requests_per_minute
            refill_rate = capacity / 60.0
        else:
            capacity = self._default_capacity
            refill_rate = self._default_refill_rate

        bucket = self._get_or_create(key, capacity=capacity, refill_rate=refill_rate)
        return bucket.consume()


@lru_cache(maxsize=1)
def get_rate_limiter() -> RateLimiter:
    """
    Return the shared ``RateLimiter`` singleton.

    Used by both the ASGI middleware and the FastAPI dependency.
    """
    return RateLimiter()


# ---------------------------------------------------------------------------
# ASGI Middleware (blanket per-IP / per-user-id limit)
# ---------------------------------------------------------------------------


def _extract_user_id_from_token(authorization: str) -> Optional[str]:
    """
    Decode the JWT payload without signature verification to extract ``sub``.

    This is intentionally unverified — it is used only to identify the rate
    limit bucket.  Full verification happens in ``get_current_user()``.

    Args:
        authorization: Raw ``Authorization`` header value.

    Returns:
        ``user_id`` string from the ``sub`` claim, or ``None`` on failure.
    """
    if not authorization.startswith("Bearer "):
        return None
    token = authorization[len("Bearer "):]
    try:
        payload = pyjwt.decode(
            token,
            options={"verify_signature": False},
            algorithms=["HS256"],
        )
        return payload.get("sub")
    except Exception:
        return None


class RateLimiterMiddleware(BaseHTTPMiddleware):
    """
    ASGI middleware that applies a per-user token-bucket rate limit.

    Applied early in the middleware stack (before auth) to protect against
    unauthenticated abuse.  Identifies users by ``sub`` from the JWT (without
    signature check) and falls back to client IP for unauthenticated requests.

    Returns HTTP 429 with a ``Retry-After`` header when the limit is exceeded.
    """

    def __init__(self, app: ASGIApp, limiter: Optional["RateLimiter"] = None) -> None:
        """
        Args:
            app:     The wrapped ASGI application.
            limiter: Optional ``RateLimiter`` instance.  Defaults to the
                     process-wide singleton from ``get_rate_limiter()``.
                     Pass an explicit instance in tests to avoid shared state.
        """
        super().__init__(app)
        self._limiter = limiter if limiter is not None else get_rate_limiter()

    async def dispatch(self, request: Request, call_next: object) -> Response:
        """
        Check rate limit and pass request through or return 429.

        Args:
            request:   Incoming HTTP request.
            call_next: Next ASGI handler in the chain.

        Returns:
            The downstream response, or a 429 JSON response on limit exceeded.
        """
        auth_header: str = request.headers.get("Authorization", "")
        user_id = _extract_user_id_from_token(auth_header)
        bucket_key = user_id or (request.client.host if request.client else "unknown")

        allowed, retry_after = self._limiter.check(bucket_key)

        if not allowed:
            retry_seconds = int(retry_after) + 1
            logger.warning(
                "Rate limit exceeded",
                extra={
                    "bucket_key": bucket_key,
                    "retry_after_seconds": retry_seconds,
                    "path": request.url.path,
                },
            )
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Please slow down."},
                headers={"Retry-After": str(retry_seconds)},
            )

        return await call_next(request)


# ---------------------------------------------------------------------------
# FastAPI Dependency (tier-aware, runs after full authentication)
# ---------------------------------------------------------------------------


async def rate_limit(
    request: Request,
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    """
    FastAPI dependency that applies per-tier rate limiting after authentication.

    Compose with ``get_current_user`` on routes that need precise tier-based
    limits.  The middleware already blocks severe abuse; this dependency
    enforces the correct per-tier ceiling.

    Args:
        request:      Incoming HTTP request (used for path logging).
        current_user: Authenticated user (injected by FastAPI).

    Raises:
        HTTPException 429: With ``Retry-After`` header when limit exceeded.
    """
    limiter = get_rate_limiter()
    allowed, retry_after = limiter.check(current_user.id, tier=current_user.tier)

    if not allowed:
        retry_seconds = int(retry_after) + 1
        logger.warning(
            "Tier rate limit exceeded",
            extra={
                "user_id": current_user.id,
                "tier": current_user.tier.name,
                "retry_after_seconds": retry_seconds,
                "path": request.url.path,
            },
        )
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Please wait before making more requests.",
            headers={"Retry-After": str(retry_seconds)},
        )
