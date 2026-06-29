"""
platforms/__init__.py — Platform Connector Registry and Factory

Provides get_connector() — the single entry point for instantiating platform
connectors.  All callers (API routers, workers) must use this factory rather
than importing connector classes directly.

Adding a new platform:
  1. Create platforms/<name>.py implementing BasePlatformConnector.
  2. Import the class here and add it to _CONNECTOR_REGISTRY.
  3. Add the platform to the CHECK constraint in 001_initial_schema.sql.

Currently built connectors:
  - amazon (AmazonConnector) ✅

Stubs (not yet implemented):
  - etsy, shopify, ebay, woocommerce
"""

from __future__ import annotations

from platforms.base import BasePlatformConnector
from platforms.amazon import AmazonConnector

_CONNECTOR_REGISTRY: dict[str, type[BasePlatformConnector]] = {
    "amazon": AmazonConnector,
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
