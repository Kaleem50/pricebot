"""
api/middleware/auth_guard.py — JWT Validation and Tier Enforcement

Provides:
  - ``require_tier()`` — dependency factory that gates routes by subscription tier.

JWT validation itself lives in ``api/dependencies.get_current_user()``.  Routes
that only need auth (any valid token) use ``get_current_user`` directly.  Routes
that additionally require a minimum tier use ``require_tier()`` as a dependency.

Usage::

    from api.middleware.auth_guard import require_tier
    from api.dependencies import AuthenticatedUser, Tier, get_current_user

    @router.post("/products/{id}/auto-reprice")
    async def enable_auto_reprice(
        product_id: str,
        _: None = Depends(require_tier(Tier.GROWTH)),   # Starter cannot auto-reprice
        current_user: AuthenticatedUser = Depends(get_current_user),
    ):
        ...

Security constraints (SECURITY.md §2.3):
  - Tier is read from the ``subscriptions`` table via ``get_current_user()``,
    never from the request body.
  - HTTP 401 for invalid/expired tokens (raised by ``get_current_user``).
  - HTTP 403 for insufficient tier (raised by ``require_tier``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastapi import Depends, HTTPException

from api.dependencies import AuthenticatedUser, Tier, get_current_user

logger = logging.getLogger(__name__)


def require_tier(minimum_tier: Tier) -> Callable:
    """
    FastAPI dependency factory that enforces a minimum subscription tier.

    Wraps ``get_current_user`` so the JWT is validated first.  If the user's
    tier is below ``minimum_tier``, raises HTTP 403 with a user-facing message
    that names the required plan.

    Args:
        minimum_tier: The lowest ``Tier`` that may access the route.

    Returns:
        A FastAPI dependency function that resolves to ``None`` on success.

    Raises:
        HTTPException 401: Propagated from ``get_current_user`` on bad token.
        HTTPException 403: Propagated from ``get_current_user`` on inactive sub,
                           or raised here when tier is insufficient.

    Example::

        @router.delete("/platforms/{platform}")
        async def disconnect_platform(
            platform: str,
            _: None = Depends(require_tier(Tier.STARTER)),  # all tiers can disconnect
            current_user: AuthenticatedUser = Depends(get_current_user),
        ):
            ...
    """
    async def _check_tier(
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> None:
        """Validate that the current user meets the minimum tier requirement."""
        if current_user.tier < minimum_tier:
            tier_name = minimum_tier.name.capitalize()
            logger.info(
                "Tier enforcement: access denied",
                extra={
                    "user_id": current_user.id,
                    "user_tier": current_user.tier.name,
                    "required_tier": minimum_tier.name,
                },
            )
            raise HTTPException(
                status_code=403,
                detail=(
                    f"This feature requires the {tier_name} plan or higher. "
                    f"Your current plan is {current_user.tier.name.capitalize()}."
                ),
            )

    return _check_tier
