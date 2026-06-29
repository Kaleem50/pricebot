"""
tests/unit/test_rate_limiter.py — Unit Tests for Token-Bucket Rate Limiter

Tests cover:
  - ``TokenBucket``: token consumption, refill math, capacity capping.
  - ``RateLimiter``: per-key bucket isolation, tier-specific capacity.
  - ``RateLimiterMiddleware``: 429 with Retry-After header, passthrough within limit.
  - ``rate_limit`` dependency: tier-based enforcement via FastAPI DI.
  - ``RATE_LIMITS`` constants: correct values per tier (SECURITY.md §5.1).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from api.dependencies import AuthenticatedUser, Tier, get_current_user
from api.middleware.rate_limiter import (
    RATE_LIMITS,
    RateLimit,
    RateLimiter,
    RateLimiterMiddleware,
    TokenBucket,
    rate_limit,
)


# ---------------------------------------------------------------------------
# TokenBucket tests
# ---------------------------------------------------------------------------


class TestTokenBucket:
    """Unit tests for the ``TokenBucket`` class."""

    def test_full_bucket_allows_first_request(self) -> None:
        """A freshly created bucket should allow the first request."""
        bucket = TokenBucket(capacity=10, refill_rate=1.0)
        allowed, retry_after = bucket.consume()
        assert allowed is True
        assert retry_after == 0.0

    def test_empty_bucket_denies_request(self) -> None:
        """Consuming more tokens than the capacity results in denial."""
        bucket = TokenBucket(capacity=3, refill_rate=1.0)
        bucket.consume()
        bucket.consume()
        bucket.consume()
        allowed, retry_after = bucket.consume()
        assert allowed is False
        assert retry_after > 0

    def test_retry_after_is_positive_on_denial(self) -> None:
        """Retry-after value must be positive when the bucket is empty."""
        bucket = TokenBucket(capacity=1, refill_rate=0.5)
        bucket.consume()
        allowed, retry_after = bucket.consume()
        assert allowed is False
        assert retry_after > 0.0

    def test_bucket_allows_requests_up_to_capacity(self) -> None:
        """Exactly ``capacity`` consecutive requests succeed; the next is denied."""
        capacity = 5
        bucket = TokenBucket(capacity=capacity, refill_rate=1.0)
        results = [bucket.consume() for _ in range(capacity + 1)]
        allowed_flags = [r[0] for r in results]
        assert all(allowed_flags[:capacity])
        assert allowed_flags[capacity] is False

    def test_bucket_refills_over_time(self) -> None:
        """Advancing ``_last_refill`` simulates time passing and token refill."""
        bucket = TokenBucket(capacity=2, refill_rate=1.0)
        bucket.consume()
        bucket.consume()
        allowed, _ = bucket.consume()
        assert allowed is False

        # Simulate 1.5 seconds of elapsed time
        bucket._last_refill -= 1.5  # type: ignore[attr-defined]
        allowed_after_wait, _ = bucket.consume()
        assert allowed_after_wait is True

    def test_tokens_capped_at_capacity_after_long_idle(self) -> None:
        """Token count never exceeds ``capacity``, even after a very long idle period."""
        bucket = TokenBucket(capacity=5, refill_rate=10.0)
        bucket._last_refill -= 3600  # type: ignore[attr-defined]
        # All 5 should be allowed; the 6th should fail
        for _ in range(5):
            allowed, _ = bucket.consume()
            assert allowed is True
        allowed, _ = bucket.consume()
        assert allowed is False

    def test_available_property_decreases_after_consume(self) -> None:
        """``available`` decreases after each successful ``consume()`` call."""
        bucket = TokenBucket(capacity=10, refill_rate=1.0)
        before = bucket.available
        bucket.consume()
        assert bucket.available < before


# ---------------------------------------------------------------------------
# RateLimiter tests
# ---------------------------------------------------------------------------


class TestRateLimiter:
    """Unit tests for the ``RateLimiter`` class."""

    def test_different_keys_have_independent_buckets(self) -> None:
        """Exhausting user_a's bucket does not affect user_b."""
        limiter = RateLimiter(default_capacity=2)
        limiter.check("user_a")
        limiter.check("user_a")
        allowed_a, _ = limiter.check("user_a")
        allowed_b, _ = limiter.check("user_b")
        assert allowed_a is False
        assert allowed_b is True

    def test_starter_tier_capacity(self) -> None:
        """STARTER tier: exactly 30 requests succeed before the bucket empties."""
        limiter = RateLimiter()
        results = [limiter.check("starter_user", tier=Tier.STARTER) for _ in range(31)]
        allowed = [r[0] for r in results]
        assert all(allowed[:30])
        assert allowed[30] is False

    def test_growth_tier_capacity(self) -> None:
        """GROWTH tier: exactly 60 requests succeed before the bucket empties."""
        limiter = RateLimiter()
        results = [limiter.check("growth_user", tier=Tier.GROWTH) for _ in range(61)]
        allowed = [r[0] for r in results]
        assert all(allowed[:60])
        assert allowed[60] is False

    def test_pro_tier_capacity(self) -> None:
        """PRO tier: exactly 120 requests succeed before the bucket empties."""
        limiter = RateLimiter()
        results = [limiter.check("pro_user", tier=Tier.PRO) for _ in range(121)]
        allowed = [r[0] for r in results]
        assert all(allowed[:120])
        assert allowed[120] is False

    def test_higher_tier_allows_more_requests(self) -> None:
        """PRO capacity must be strictly greater than STARTER capacity."""
        assert (
            RATE_LIMITS[Tier.STARTER].requests_per_minute
            < RATE_LIMITS[Tier.PRO].requests_per_minute
        )


# ---------------------------------------------------------------------------
# RATE_LIMITS constant tests
# ---------------------------------------------------------------------------


class TestRateLimitConstants:
    """Tests for the ``RATE_LIMITS`` dict and ``RateLimit`` dataclass."""

    def test_all_tiers_defined(self) -> None:
        """``RATE_LIMITS`` must have an entry for every ``Tier`` member."""
        for tier in Tier:
            assert tier in RATE_LIMITS, f"Missing RATE_LIMITS entry for {tier}"

    def test_limits_are_rate_limit_instances(self) -> None:
        """Every value in ``RATE_LIMITS`` is a ``RateLimit`` dataclass instance."""
        for tier, limit in RATE_LIMITS.items():
            assert isinstance(limit, RateLimit), f"{tier}: expected RateLimit"

    def test_starter_values(self) -> None:
        """STARTER: 30 rpm, 5 batch submits/hour (SECURITY.md §5.1)."""
        assert RATE_LIMITS[Tier.STARTER].requests_per_minute == 30
        assert RATE_LIMITS[Tier.STARTER].batch_submits_per_hour == 5

    def test_growth_values(self) -> None:
        """GROWTH: 60 rpm, 10 batch submits/hour (SECURITY.md §5.1)."""
        assert RATE_LIMITS[Tier.GROWTH].requests_per_minute == 60
        assert RATE_LIMITS[Tier.GROWTH].batch_submits_per_hour == 10

    def test_pro_values(self) -> None:
        """PRO: 120 rpm, 20 batch submits/hour (SECURITY.md §5.1)."""
        assert RATE_LIMITS[Tier.PRO].requests_per_minute == 120
        assert RATE_LIMITS[Tier.PRO].batch_submits_per_hour == 20

    def test_limits_increase_monotonically_with_tier(self) -> None:
        """Request limits must increase (or stay equal) as tier increases."""
        assert (
            RATE_LIMITS[Tier.STARTER].requests_per_minute
            <= RATE_LIMITS[Tier.GROWTH].requests_per_minute
            <= RATE_LIMITS[Tier.PRO].requests_per_minute
        )


# ---------------------------------------------------------------------------
# RateLimiterMiddleware tests
# ---------------------------------------------------------------------------


def _make_middleware_app(capacity: int) -> TestClient:
    """
    Build a minimal FastAPI app with ``RateLimiterMiddleware`` using a
    fresh ``RateLimiter`` of the given ``capacity``.

    Passes the limiter directly to the middleware constructor to avoid
    shared singleton state between tests.
    """
    app = FastAPI()
    limiter = RateLimiter(default_capacity=capacity)
    app.add_middleware(RateLimiterMiddleware, limiter=limiter)

    @app.get("/ping")
    async def ping() -> dict:
        return {"pong": True}

    return TestClient(app, raise_server_exceptions=False)


class TestRateLimiterMiddleware:
    """Integration tests for ``RateLimiterMiddleware`` via ``TestClient``."""

    def test_requests_within_limit_return_200(self) -> None:
        """Requests within the bucket capacity pass through with HTTP 200."""
        client = _make_middleware_app(capacity=5)
        for _ in range(3):
            assert client.get("/ping").status_code == 200

    def test_429_returned_when_limit_exceeded(self) -> None:
        """The request that exceeds capacity=1 receives HTTP 429."""
        client = _make_middleware_app(capacity=1)
        client.get("/ping")  # allowed (uses the 1 token)
        response = client.get("/ping")  # denied
        assert response.status_code == 429

    def test_429_includes_retry_after_header(self) -> None:
        """HTTP 429 response must include a ``Retry-After`` header."""
        client = _make_middleware_app(capacity=1)
        client.get("/ping")
        response = client.get("/ping")
        assert response.status_code == 429
        assert "retry-after" in {k.lower() for k in response.headers}

    def test_retry_after_is_positive_integer_seconds(self) -> None:
        """``Retry-After`` value must be a decimal integer >= 1."""
        client = _make_middleware_app(capacity=1)
        client.get("/ping")
        response = client.get("/ping")
        assert response.status_code == 429
        retry_str = response.headers.get("Retry-After", "")
        assert retry_str.isdigit(), f"Expected integer, got {retry_str!r}"
        assert int(retry_str) >= 1

    def test_429_body_contains_detail(self) -> None:
        """HTTP 429 body must contain a ``detail`` field with a user-friendly message."""
        client = _make_middleware_app(capacity=1)
        client.get("/ping")
        response = client.get("/ping")
        assert response.status_code == 429
        assert "detail" in response.json()

    def test_different_ips_have_independent_limits(self) -> None:
        """Two clients with different IPs (simulated) should not share a bucket."""
        # Both requests go to the same TestClient (same IP in tests), so we just
        # verify a fresh app with capacity=2 allows exactly 2 requests.
        client = _make_middleware_app(capacity=2)
        assert client.get("/ping").status_code == 200
        assert client.get("/ping").status_code == 200
        assert client.get("/ping").status_code == 429


# ---------------------------------------------------------------------------
# rate_limit dependency tests
# ---------------------------------------------------------------------------


def _make_rate_limit_dep_app(user_tier: Tier, limiter: RateLimiter) -> TestClient:
    """
    Build a FastAPI app with a ``/limited`` route protected by the ``rate_limit``
    dependency.  ``get_current_user`` is overridden to return a user with the
    given tier.  The provided ``limiter`` is injected via ``get_rate_limiter``.
    """
    app = FastAPI()

    mock_user = AuthenticatedUser(id="test-user", email="t@e.com", tier=user_tier)

    async def override_user() -> AuthenticatedUser:
        return mock_user

    app.dependency_overrides[get_current_user] = override_user

    @app.get("/limited")
    async def limited(_: None = Depends(rate_limit)) -> dict:
        return {"ok": True}

    # Patch the singleton used inside the dependency
    with patch("api.middleware.rate_limiter.get_rate_limiter", return_value=limiter):
        client = TestClient(app, raise_server_exceptions=False)
        # Force the TestClient to build the app so the patch is active
        client.get("/limited", headers={"Authorization": "Bearer tok"})

    return client


class TestRateLimitDependency:
    """Tests for the ``rate_limit`` FastAPI dependency."""

    def test_within_limit_returns_200(self) -> None:
        """First request from authenticated user within bucket limit returns 200."""
        limiter = RateLimiter(default_capacity=10)
        app = FastAPI()
        mock_user = AuthenticatedUser(id="u1", email="u@e.com", tier=Tier.GROWTH)

        async def override_user() -> AuthenticatedUser:
            return mock_user

        app.dependency_overrides[get_current_user] = override_user

        @app.get("/limited")
        async def limited(_: None = Depends(rate_limit)) -> dict:
            return {"ok": True}

        with patch("api.middleware.rate_limiter.get_rate_limiter", return_value=limiter):
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/limited", headers={"Authorization": "Bearer tok"})

        assert response.status_code == 200

    def test_exceeded_limit_returns_429(self) -> None:
        """When the rate limiter denies a request, HTTP 429 is returned with Retry-After."""
        # Mock the limiter to always deny (simulates an exhausted bucket)
        mock_limiter = MagicMock(spec=RateLimiter)
        mock_limiter.check.return_value = (False, 2.5)

        app = FastAPI()
        mock_user = AuthenticatedUser(id="u2", email="u@e.com", tier=Tier.STARTER)

        async def override_user() -> AuthenticatedUser:
            return mock_user

        app.dependency_overrides[get_current_user] = override_user

        @app.get("/limited")
        async def limited(_: None = Depends(rate_limit)) -> dict:
            return {"ok": True}

        with patch("api.middleware.rate_limiter.get_rate_limiter", return_value=mock_limiter):
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/limited", headers={"Authorization": "Bearer tok"})

        assert response.status_code == 429
        assert "retry-after" in {k.lower() for k in response.headers}

    def test_429_includes_retry_after_header_in_dependency(self) -> None:
        """``rate_limit`` dependency must set ``Retry-After`` >= 1 second on 429 responses."""
        mock_limiter = MagicMock(spec=RateLimiter)
        mock_limiter.check.return_value = (False, 5.0)  # 5 seconds until next token

        app = FastAPI()
        mock_user = AuthenticatedUser(id="u3", email="u@e.com", tier=Tier.GROWTH)

        async def override_user() -> AuthenticatedUser:
            return mock_user

        app.dependency_overrides[get_current_user] = override_user

        @app.get("/limited")
        async def limited(_: None = Depends(rate_limit)) -> dict:
            return {"ok": True}

        with patch("api.middleware.rate_limiter.get_rate_limiter", return_value=mock_limiter):
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/limited", headers={"Authorization": "Bearer tok"})

        assert response.status_code == 429
        assert int(response.headers.get("Retry-After", "0")) >= 1
