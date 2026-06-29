"""
tests/fixtures/amazon_responses.py — Mock Amazon SP-API Response Fixtures

Pre-built response dicts that mirror the structure returned by real Amazon
SP-API endpoints.  Used by test_amazon_connector.py to avoid live API calls.

All data is synthetic — prices, ASINs, and SKUs are not real.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# LWA token exchange
# ---------------------------------------------------------------------------

LWA_TOKEN_SUCCESS = {
    "access_token": "Atza|test-access-token-abc123",
    "expires_in": 3600,
    "token_type": "bearer",
}

LWA_TOKEN_INVALID_GRANT = {
    "error": "invalid_grant",
    "error_description": "The provided refresh token has been used already.",
}

# ---------------------------------------------------------------------------
# Listings Items API  (/listings/2021-08-01/items/{sellerId})
# ---------------------------------------------------------------------------

LISTINGS_PAGE_1 = {
    "numberOfResults": 2,
    "nextPageToken": "page-2-token",
    "items": [
        {
            "sku": "SKU-001",
            "asin": "B001TEST01",
            "summaries": [{"itemName": "Widget Pro 500ml", "marketplaceId": "ATVPDKIKX0DER"}],
            "attributes": {
                "purchasable_offer": [
                    {
                        "marketplace_id": "ATVPDKIKX0DER",
                        "our_price": [{"schedule": [{"value_with_tax": 24.99}]}],
                    }
                ]
            },
        },
    ],
}

LISTINGS_PAGE_2 = {
    "numberOfResults": 2,
    "items": [
        {
            "sku": "SKU-002",
            "asin": "B002TEST02",
            "summaries": [{"itemName": "Gadget Basic 1L", "marketplaceId": "ATVPDKIKX0DER"}],
            "attributes": {
                "purchasable_offer": [
                    {
                        "marketplace_id": "ATVPDKIKX0DER",
                        "our_price": [{"schedule": [{"value_with_tax": 14.99}]}],
                    }
                ]
            },
        },
    ],
}

LISTINGS_SINGLE = {
    "numberOfResults": 1,
    "items": [
        {
            "sku": "SKU-001",
            "asin": "B001TEST01",
            "summaries": [{"itemName": "Widget Pro 500ml", "marketplaceId": "ATVPDKIKX0DER"}],
            "attributes": {
                "purchasable_offer": [
                    {
                        "marketplace_id": "ATVPDKIKX0DER",
                        "our_price": [{"schedule": [{"value_with_tax": 24.99}]}],
                    }
                ]
            },
        }
    ],
}

LISTINGS_EMPTY = {"numberOfResults": 0, "items": []}

# ---------------------------------------------------------------------------
# Competitive Pricing API  (/products/pricing/2022-05-01/competitivePrice)
# ---------------------------------------------------------------------------

COMPETITIVE_PRICING_SUCCESS = {
    "payload": [
        {
            "ASIN": "B001TEST01",
            "status": "Success",
            "Product": {
                "Identifiers": {
                    "MarketplaceASIN": {
                        "MarketplaceId": "ATVPDKIKX0DER",
                        "ASIN": "B001TEST01",
                    }
                },
                "CompetitivePricing": {
                    "CompetitivePrices": [
                        {
                            "CompetitivePriceId": "1",
                            "Price": {
                                "LandedPrice": {"CurrencyCode": "USD", "Amount": 22.99},
                                "ListingPrice": {"CurrencyCode": "USD", "Amount": 22.99},
                            },
                            "condition": "New",
                            "belongsToRequester": False,
                        },
                        {
                            "CompetitivePriceId": "2",
                            "Price": {
                                "LandedPrice": {"CurrencyCode": "USD", "Amount": 23.49},
                                "ListingPrice": {"CurrencyCode": "USD", "Amount": 23.49},
                            },
                            "condition": "New",
                            "belongsToRequester": False,
                        },
                    ],
                    "NumberOfItemsSold": 15,
                    "NumberOfItemsForSale": 8,
                },
            },
        }
    ]
}

COMPETITIVE_PRICING_NO_COMPETITORS = {
    "payload": [
        {
            "ASIN": "B001TEST01",
            "status": "Success",
            "Product": {
                "Identifiers": {
                    "MarketplaceASIN": {"MarketplaceId": "ATVPDKIKX0DER", "ASIN": "B001TEST01"}
                },
                "CompetitivePricing": {
                    "CompetitivePrices": [],
                    "NumberOfItemsSold": 0,
                    "NumberOfItemsForSale": 0,
                },
            },
        }
    ]
}

COMPETITIVE_PRICING_RATE_LIMITED = {}  # 429 response has empty body

# ---------------------------------------------------------------------------
# Listings Items PATCH  (price update)
# ---------------------------------------------------------------------------

LISTINGS_PATCH_SUCCESS = {
    "sku": "SKU-001",
    "status": "ACCEPTED",
    "submissionId": "submission-abc-123",
    "issues": [],
}

LISTINGS_PATCH_RATE_LIMITED = {}  # 429 has empty body

LISTINGS_PATCH_NOT_FOUND = {
    "errors": [
        {
            "code": "NOT_FOUND",
            "message": "SKU not found",
            "details": "",
        }
    ]
}


# ---------------------------------------------------------------------------
# Helpers for test setup
# ---------------------------------------------------------------------------


def make_credentials(
    *,
    refresh_token: str = "test-refresh-token",
    client_id: str = "test-client-id",
    client_secret: str = "test-client-secret",
    marketplace_id: str = "ATVPDKIKX0DER",
    merchant_id: str = "TEST_MERCHANT_123",
) -> dict[str, str]:
    """Return a complete Amazon credentials dict for test instantiation."""
    return {
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "marketplace_id": marketplace_id,
        "merchant_id": merchant_id,
    }
