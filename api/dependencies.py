"""
api/dependencies.py — Shared FastAPI Dependencies

Defines the core data models and dependency functions used across all routers:

  - ``Tier``              — IntEnum for subscription tiers (ordinal comparison enabled).
  - ``AuthenticatedUser`` — Pydantic v2 model populated from a validated JWT.
  - ``get_db()``          — FastAPI dependency wrapping the Supabase singleton.
  - ``get_current_user()``— FastAPI dependency that validates the Bearer JWT,
                            extracts ``user_id`` from token claims only (never from
                            the request body), and returns an ``AuthenticatedUser``.

Security constraints (SECURITY.md §2):
  - ``user_id`` is always sourced from ``payload["sub"]`` of the validated JWT.
  - Tier is sourced from the ``subscriptions`` table, never from the client.
  - Expired or malformed tokens raise HTTP 401.
  - Inactive subscriptions raise HTTP 403.
"""

from __future__ import annotations

import logging
from enum import IntEnum

from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel
from supabase import Client

from db.client import get_auth_client as _get_auth_client
from db.client import get_db as _get_db_singleton

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tier
# ---------------------------------------------------------------------------


class Tier(IntEnum):
    """
    Subscription tier in ascending capability order.

    Integer values enable direct comparison in ``require_tier()``::

        if current_user.tier < Tier.GROWTH:
            raise HTTPException(status_code=403, ...)

    DB values are lowercase strings: 'starter', 'growth', 'pro'.
    Use ``Tier.from_db()`` to parse them.
    """

    STARTER = 1
    GROWTH = 2
    PRO = 3

    @classmethod
    def from_db(cls, value: str) -> "Tier":
        """
        Parse a lowercase DB tier string into a ``Tier`` enum member.

        Args:
            value: One of 'starter', 'growth', 'pro'.

        Returns:
            Corresponding ``Tier`` member.

        Raises:
            ValueError: If ``value`` is not a recognised tier string.
        """
        mapping: dict[str, "Tier"] = {
            "starter": cls.STARTER,
            "growth": cls.GROWTH,
            "pro": cls.PRO,
        }
        try:
            return mapping[value.lower()]
        except KeyError:
            raise ValueError(f"Unknown tier value from DB: {value!r}")


# ---------------------------------------------------------------------------
# AuthenticatedUser
# ---------------------------------------------------------------------------


class AuthenticatedUser(BaseModel):
    """
    Authenticated and authorised user extracted from a validated JWT.

    Populated by ``get_current_user()`` and injected into protected routes.
    The ``id`` field is always sourced from ``payload['sub']`` — it is never
    accepted from the request body or query parameters.
    """

    id: str
    email: str
    tier: Tier

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# FastAPI Dependencies
# ---------------------------------------------------------------------------


def get_db() -> Client:
    """
    FastAPI dependency that returns the shared Supabase client singleton.

    Thin wrapper around ``db.client.get_db()`` to allow dependency injection
    in route handlers.

    Never call ``auth.sign_up()``, ``auth.sign_in_with_password()``, or
    ``auth.refresh_session()`` on the client this returns — use
    ``get_auth_db()`` instead. See ``db/client.py`` module docstring for why.

    Returns:
        Supabase ``Client`` instance.
    """
    return _get_db_singleton()


def get_auth_db() -> Client:
    """
    FastAPI dependency that returns a fresh, request-scoped Supabase client
    for end-user auth operations only: ``sign_up()``, ``sign_in_with_password()``,
    ``refresh_session()``.

    Thin wrapper around ``db.client.get_auth_client()``. Deliberately never
    the same instance as ``get_db()`` — see ``db/client.py`` module docstring
    for the Critical bug this separation fixes (shared-singleton session
    contamination across concurrent requests).

    Returns:
        A new Supabase ``Client`` instance, not cached.
    """
    return _get_auth_client()


async def get_current_user(
    authorization: str = Header(..., alias="Authorization"),
    db: Client = Depends(get_db),
) -> AuthenticatedUser:
    """
    Validate the Bearer JWT and return the authenticated user.

    Uses Supabase's built-in auth.get_user() which correctly verifies ES256
    JWT signatures against Supabase's public keys.  Extracts user_id and email
    from the verified session. Then queries the subscriptions table to get
    the user's active tier.

    Args:
        authorization: Raw ``Authorization`` header value.
        db:            Supabase client (injected).

    Returns:
        ``AuthenticatedUser`` with ``id``, ``email``, and ``tier``.

    Raises:
        HTTPException 401: Missing header, malformed token, or expired token.
        HTTPException 403: Subscription is inactive or cancelled.
        HTTPException 500: Unexpected DB error during subscription lookup.
    """
    if not authorization.startswith("Bearer "):
        logger.warning("Invalid Authorization header format")
        raise HTTPException(
            status_code=401,
            detail="Authorization header must be 'Bearer <token>'",
        )

    token = authorization[len("Bearer "):]

    # Use Supabase's built-in auth.get_user() to verify ES256 JWT correctly.
    # Returns UserResponse with a .user attribute — access .user.id, not .id.
    try:
        user_response = db.auth.get_user(token)
    except Exception as exc:
        logger.warning("JWT validation failed", extra={"error": str(exc)})
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user = getattr(user_response, "user", user_response)
    if not user:
        logger.warning("auth.get_user() returned no user")
        raise HTTPException(status_code=401, detail="Token verification failed")

    user_id: str = user.id
    email: str = user.email or ""

    try:
        result = (
            db.table("subscriptions")
            .select("tier, status")
            .eq("user_id", user_id)
            .execute()
        )
    except Exception as exc:
        logger.error(
            "Subscription lookup failed",
            extra={"user_id": user_id, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Authentication service error")

    if not result.data:
        # New user with no subscription record yet — default to Starter.
        tier = Tier.STARTER
    else:
        row = result.data[0]
        if row["status"] not in ("active", "trialing"):
            logger.info(
                "Subscription inactive — access denied",
                extra={"user_id": user_id, "status": row["status"]},
            )
            raise HTTPException(
                status_code=403,
                detail="Your subscription is inactive. Please visit billing to reactivate.",
            )
        try:
            tier = Tier.from_db(row["tier"])
        except ValueError:
            logger.error(
                "Unknown tier value in subscriptions table",
                extra={"user_id": user_id, "tier_value": row["tier"]},
            )
            tier = Tier.STARTER

    return AuthenticatedUser(id=user_id, email=email, tier=tier)
