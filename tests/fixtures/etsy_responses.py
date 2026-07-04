"""
tests/fixtures/etsy_responses.py — Mock Etsy Open API v3 Response Fixtures

Pre-built response dicts that mirror the structure returned by the real Etsy
API endpoints. Used by test_etsy_connector.py to avoid live API calls.

All data is synthetic — listing IDs, prices, and shop IDs are not real.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# OAuth token refresh
# ---------------------------------------------------------------------------

TOKEN_REFRESH_SUCCESS = {
    "access_token": "etsy-refreshed-access-token-xyz789",
    "token_type": "Bearer",
    "expires_in": 3600,
    "refresh_token": "etsy-new-refresh-token-abc456",
}

TOKEN_REFRESH_INVALID_GRANT = {
    "error": "invalid_grant",
    "error_description": "refresh_token is expired",
}

# ---------------------------------------------------------------------------
# /users/me  (validate credentials)
# ---------------------------------------------------------------------------

USERS_ME_SUCCESS = {
    "user_id": 12345678,
    "login_name": "TestSeller",
    "primary_email": "test@example.com",
}

# ---------------------------------------------------------------------------
# Active listings — single page
# ---------------------------------------------------------------------------

LISTINGS_SINGLE_PAGE = {
    "count": 2,
    "results": [
        {
            "listing_id": 100000001,
            "state": "active",
            "title": "Handmade Ceramic Coffee Mug",
            "price": {"amount": 2800, "divisor": 100, "currency_code": "USD"},
            "url": "https://www.etsy.com/listing/100000001/handmade-ceramic-coffee-mug",
        },
        {
            "listing_id": 100000002,
            "state": "active",
            "title": "Custom Engraved Wooden Cutting Board",
            "price": {"amount": 4500, "divisor": 100, "currency_code": "USD"},
            "url": "https://www.etsy.com/listing/100000002/custom-engraved-wooden-cutting-board",
        },
    ],
}

# ---------------------------------------------------------------------------
# Active listings — page 1 of 2 (for pagination tests)
# ---------------------------------------------------------------------------

LISTINGS_PAGE_1 = {
    "count": 150,
    "results": [
        {
            "listing_id": 200000001,
            "state": "active",
            "title": "Knitted Winter Scarf",
            "price": {"amount": 3500, "divisor": 100, "currency_code": "USD"},
            "url": "https://www.etsy.com/listing/200000001/knitted-winter-scarf",
        },
    ]
    * 100,  # 100 results = full page, more pages to follow
}

# ---------------------------------------------------------------------------
# Active listings — page 2 of 2 (for pagination tests)
# ---------------------------------------------------------------------------

LISTINGS_PAGE_2 = {
    "count": 150,
    "results": [
        {
            "listing_id": 200000002,
            "state": "active",
            "title": "Hand-Painted Watercolour Print",
            "price": {"amount": 1999, "divisor": 100, "currency_code": "USD"},
            "url": "https://www.etsy.com/listing/200000002/hand-painted-watercolour-print",
        },
    ]
    * 50,  # 50 results = partial page, no more pages
}

# ---------------------------------------------------------------------------
# Competitor search results  (GET /listings/active?keywords=...)
# ---------------------------------------------------------------------------

COMPETITOR_SEARCH_RESULTS = {
    "count": 5,
    "results": [
        {
            "listing_id": 300000001,
            "state": "active",
            "title": "Handmade Ceramic Coffee Mug Blue",
            "price": {"amount": 2200, "divisor": 100, "currency_code": "USD"},
            # Different shop — should be included as competitor
            "url": "https://www.etsy.com/listing/300000001/handmade-ceramic-coffee-mug-blue",
        },
        {
            "listing_id": 300000002,
            "state": "active",
            "title": "Handmade Ceramic Coffee Mug Red",
            "price": {"amount": 2500, "divisor": 100, "currency_code": "USD"},
            "url": "https://www.etsy.com/listing/300000002/handmade-ceramic-coffee-mug-red",
        },
        {
            "listing_id": 100000001,  # seller's own listing — should be FILTERED OUT
            "state": "active",
            "title": "Handmade Ceramic Coffee Mug",
            "price": {"amount": 2800, "divisor": 100, "currency_code": "USD"},
            # Contains seller's shop_id 99999999 — filter logic checks for this
            "url": "https://www.etsy.com/shop/99999999/listing/100000001",
        },
        {
            "listing_id": 300000003,
            "state": "active",
            "title": "Rustic Ceramic Coffee Mug",
            "price": {"amount": 3000, "divisor": 100, "currency_code": "USD"},
            "url": "https://www.etsy.com/listing/300000003/rustic-ceramic-coffee-mug",
        },
    ],
}

# ---------------------------------------------------------------------------
# Price update (PATCH /shops/{shop_id}/listings/{listing_id})
# ---------------------------------------------------------------------------

PATCH_SUCCESS = {"listing_id": 100000001, "price": {"amount": 2600, "divisor": 100}}

# ---------------------------------------------------------------------------
# Credentials helper
# ---------------------------------------------------------------------------


def make_credentials(
    access_token: str = "etsy-test-access-token",
    refresh_token: str = "etsy-test-refresh-token",
    shop_id: str = "99999999",
) -> dict[str, str]:
    """Build a credentials dict for testing."""
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "shop_id": shop_id,
    }
