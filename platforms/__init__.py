"""
platforms/__init__.py — Platform Connector Registry and Factory

Provides get_connector() — the single entry point for instantiating platform
connectors.  All callers (API routers, workers) must use this factory rather
than importing connector classes directly.

Mock Connector (Development Only):
  When environment variable MOCK_PLATFORM_MODE=true, get_connector() returns
  MockConnector for all platforms. This enables end-to-end testing of the
  worker pipeline without real platform credentials. MockConnector is never
  active in production (guarded by startup assertions in api/main.py and
  workers/scheduler.py).

Adding a new platform:
  1. Create platforms/<name>.py implementing BasePlatformConnector.
  2. Import the class here and add it to _CONNECTOR_REGISTRY.
  3. Add the platform to the CHECK constraint in 001_initial_schema.sql.

Currently built connectors:
  - amazon (AmazonConnector) ✅
  - etsy (EtsyConnector) ✅

Stubs (not yet implemented):
  - shopify, ebay, woocommerce
"""

from __future__ import annotations

import os

from platforms.base import BasePlatformConnector
from platforms.amazon import AmazonConnector
from platforms.etsy import EtsyConnector
from platforms.mock import MockConnector

_CONNECTOR_REGISTRY: dict[str, type[BasePlatformConnector]] = {
    "amazon": AmazonConnector,
    "etsy": EtsyConnector,
}

SUPPORTED_PLATFORMS = list(_CONNECTOR_REGISTRY.keys())

# All valid platform identifiers (including those not yet built)
ALL_PLATFORMS = ["amazon", "etsy", "shopify", "ebay", "woocommerce"]


def get_connector(
    platform: str,
    credentials: dict[str, str],
    user_id: str,
    db: object | None = None,
) -> BasePlatformConnector:
    """
    Instantiate the correct platform connector for the given platform.

    If MOCK_PLATFORM_MODE=true (development only), returns MockConnector
    for all platforms, bypassing real API credentials.

    Args:
        platform:    Platform identifier ('amazon', 'etsy', etc.).
        credentials: Decrypted platform credentials dict.
        user_id:     Supabase user ID of the credential owner.
        db:          Optional Supabase Client.  When provided, competitor price
                     results are cached in the products table and served from
                     cache when < 15 minutes old.  Pass db=get_db() from
                     FastAPI's DI or from the worker's DB singleton.

    Returns:
        Instantiated BasePlatformConnector subclass.

    Raises:
        NotImplementedError: If the platform is valid but connector not yet built.
        ValueError:          If the platform is not a recognised identifier.
    """
    # Return MockConnector for all platforms in test mode (development only)
    if os.environ.get("MOCK_PLATFORM_MODE", "").lower() == "true":
        return MockConnector(credentials=credentials, user_id=user_id, db=db)

    if platform not in ALL_PLATFORMS:
        raise ValueError(
            f"Unknown platform: {platform!r}. "
            f"Must be one of: {ALL_PLATFORMS}"
        )
    cls = _CONNECTOR_REGISTRY.get(platform)
    if cls is None:
        raise NotImplementedError(
            f"Platform connector for {platform!r} is not yet implemented. "
            f"Built connectors: {SUPPORTED_PLATFORMS}"
        )
    return cls(credentials=credentials, user_id=user_id, db=db)
