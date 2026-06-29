# PLATFORMS.md — Platform Connector Specifications

> Build order is validation-gated. Do not implement a connector until the founder confirms the signal threshold has been met for that platform. Current status is in Section 1.

---

## 1. Platform Build Priority

| Platform | Validation Status | Build Status | Priority |
|---|---|---|---|
| **Amazon FBA** | ✅ 3 confirmed pain signals | 🔨 Build first | P0 |
| **Etsy** | ✅ 1 confirmed pain signal | 🔜 Build second | P1 |
| **Shopify** | ⏳ Pending validation | ❌ Do not build yet | P2 |
| **eBay** | ⏳ Pending validation | ❌ Do not build yet | P3 |
| **WooCommerce** | ⏳ Pending validation | ❌ Do not build yet | P4 |

---

## 2. Abstract Connector Contract

Every platform connector implements `platforms/base.py`. No platform-specific logic leaks outside its module.

```python
class BasePlatformConnector(ABC):

    def __init__(self, credentials: dict[str, str], user_id: str) -> None:
        """
        Connectors are instantiated per job with decrypted credentials.
        Credentials must not be cached beyond the instance lifecycle.
        """
        self.user_id = user_id
        self._credentials = credentials

    @abstractmethod
    async def validate_credentials(self) -> bool:
        """Test that stored credentials work. Called on connect + daily health check."""
        ...

    @abstractmethod
    async def get_products(self) -> list[MyProduct]:
        """Return full product catalog for this user on this platform."""
        ...

    @abstractmethod
    async def get_competitor_prices(
        self,
        product: MyProduct
    ) -> list[CompetitorProduct]:
        """Return active competitor listings for a given product."""
        ...

    @abstractmethod
    async def apply_price(self, product: MyProduct, new_price: float) -> bool:
        """Push new price to platform. Raise on failure, return True on success."""
        ...
```

### 2.1 Shared Behavior (Implement in Base)
- Retry with exponential backoff on `PlatformRateLimitError` (max 3 attempts)
- Structured logging on every API call (platform, endpoint, response time, status)
- Raise `PlatformAuthError` if credentials are invalid or expired
- Map all platform-native data formats to internal `MyProduct` / `CompetitorProduct` Pydantic models before returning

---

## 3. Amazon SP-API (P0 — Active Build)

### 3.1 Auth Model — LWA (Login with Amazon)

Amazon SP-API uses a two-layer auth system:
- **App-level:** Client ID + Secret registered once in Seller Central → stored in env vars
- **Per-user:** OAuth 2.0 refresh token obtained via redirect flow → stored encrypted in DB
- **Runtime:** Short-lived access token (1 hour) exchanged from refresh token before each request

```python
# platforms/amazon.py — credentials dict structure (stored encrypted in DB)
{
    "refresh_token": "Atzr|IwEBIA...",   # Long-lived — this is the critical secret
    "merchant_id": "A1B2C3D4E5F6G7",     # Seller's Amazon Merchant ID
    "marketplace_id": "ATVPDKIKX0DER",   # Target marketplace (US default)
}
```

### 3.2 OAuth Redirect Flow (Seller Onboarding)

When a seller connects their Amazon account via the dashboard wizard:

```
Step 1 — PriceBot redirects seller to Amazon auth URL:
  https://sellercentral.amazon.com/apps/authorize/consent
    ?application_id={AMAZON_APP_CLIENT_ID}
    &state={csrf_token}           ← random per-session, validated on callback
    &version=beta

Step 2 — Seller grants permissions in Seller Central

Step 3 — Amazon redirects back to PriceBot callback URL:
  https://pricebot.io/platforms/amazon/callback
    ?spapi_oauth_code={auth_code}
    &state={csrf_token}
    &selling_partner_id={merchant_id}

Step 4 — PriceBot backend exchanges auth_code for tokens:
  POST https://api.amazon.com/auth/o2/token
  {
    "grant_type": "authorization_code",
    "code": "{auth_code}",
    "redirect_uri": "https://pricebot.io/platforms/amazon/callback",
    "client_id": AMAZON_APP_CLIENT_ID,
    "client_secret": AMAZON_APP_CLIENT_SECRET,
  }
  → Returns: { "access_token": "...", "refresh_token": "Atzr|...", "expires_in": 3600 }

Step 5 — Store refresh_token encrypted, merchant_id, marketplace_id in platform_connections
```

CSRF state token must be validated on callback. Reject any callback where state does not match.

### 3.3 Runtime Access Token Management

```python
# platforms/amazon.py
import time
from typing import ClassVar

class AmazonConnector(BasePlatformConnector):
    # Class-level cache — shared across instances, keyed by user_id
    _token_cache: ClassVar[dict[str, tuple[str, float]]] = {}

    async def _get_access_token(self) -> str:
        """
        Return a valid access token, refreshing if expired.
        Caches tokens for their 1-hour lifetime to avoid redundant LWA calls.
        """
        cached = self._token_cache.get(self.user_id)
        if cached:
            token, expires_at = cached
            if time.time() < expires_at - 60:  # 60-second buffer before expiry
                return token

        response = await self._http.post(
            "https://api.amazon.com/auth/o2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._credentials["refresh_token"],
                "client_id": settings.AMAZON_APP_CLIENT_ID,
                "client_secret": settings.AMAZON_APP_CLIENT_SECRET,
            },
        )
        if response.status_code == 400:
            raise PlatformAuthError("Amazon refresh token invalid or revoked")
        response.raise_for_status()

        data = response.json()
        token = data["access_token"]
        expires_at = time.time() + data["expires_in"]
        self._token_cache[self.user_id] = (token, expires_at)

        logger.info("Amazon access token refreshed", extra={"user_id": self.user_id})
        return token
```

### 3.4 Required SP-API Permissions (App Registration)

When registering the PriceBot app in Seller Central, request these role permissions:

| SP-API Role | Why Needed |
|---|---|
| `Selling Partner Insights` | Read seller account info, validate connection |
| `Listings Items` | Read own product catalog (titles, SKUs, current prices) |
| `Product Pricing` | Read competitive pricing data by ASIN |
| `Inventory and Order Management` | Read listing state (active/inactive) |

### 3.5 Key Endpoints

#### Own Product Catalog

```
GET https://sellingpartnerapi-na.amazon.com/listings/2021-08-01/items/{sellerId}
  ?marketplaceIds={marketplace_id}
  &pageToken={nextToken}          ← pagination
  &includedData=summaries,attributes,offers

Headers:
  x-amz-access-token: {access_token}
  x-amz-date: {ISO8601 timestamp}

Response shape (simplified):
{
  "items": [
    {
      "sku": "MY-SKU-001",
      "summaries": [{ "asin": "B08XYZ1234", "itemName": "Widget Pro", "status": ["ACTIVE"] }],
      "offers": [{ "offerType": "B2C", "price": { "listingPrice": { "amount": 24.99 } } }]
    }
  ],
  "pagination": { "nextToken": "..." }   ← null when last page
}
```

Pagination: follow `nextToken` until null. Store all items before returning — do not stream partial catalogs.

#### Competitive Pricing

```
GET https://sellingpartnerapi-na.amazon.com/products/pricing/2022-05-01/competitivePrice
  ?Asins={ASIN1,ASIN2,...}        ← up to 20 ASINs per request
  &marketplaceId={marketplace_id}
  &itemType=Asin

Response: Array of competitive price objects per ASIN, including all sellers' offers.
```

Batch ASIN lookups in groups of 20. For a 50-product Starter user: 3 API calls. For 500-product Growth user: 25 calls. Apply rate limit delays accordingly.

#### Price Update

```
PATCH https://sellingpartnerapi-na.amazon.com/listings/2021-08-01/items/{sellerId}/{sku}
  ?marketplaceIds={marketplace_id}

Body:
{
  "productType": "PRODUCT",
  "patches": [
    {
      "op": "replace",
      "path": "/attributes/purchasable_offer",
      "value": [{
        "marketplace_id": "{marketplace_id}",
        "currency": "USD",
        "our_price": [{ "schedule": [{ "value_with_tax": 22.99 }] }]
      }]
    }
  ]
}
```

Price updates require the product `sku` (seller's SKU), not the ASIN. The ASIN identifies the product; the SKU identifies the seller's specific listing of that product.

### 3.6 Rate Limits — Per-Endpoint Budgets

| Endpoint | Rate | Burst | Strategy |
|---|---|---|---|
| Listings read | 5 req/sec | 10 | Token bucket in connector |
| Competitive pricing | 1 req/sec | 1 | Sleep 1s between ASIN batches |
| Listings write (price update) | 5 req/sec | 10 | Token bucket in connector |

The price applicator writes prices sequentially per user after batch results arrive. At 5 req/sec, applying prices to 50 products takes ~10 seconds. 500 products (Growth) takes ~100 seconds — acceptable, these run asynchronously.

On receiving HTTP 429:
```python
retry_after = int(response.headers.get("x-amzn-RateLimit-Limit", "1"))
await asyncio.sleep(retry_after)
# Then retry — handled by tenacity decorator on apply_price()
```

### 3.7 Internal Data Model Mapping

```python
# platforms/amazon.py — Pydantic models for Amazon-native data

class AmazonOffer(BaseModel):
    """Raw offer data from SP-API competitive pricing response."""
    seller_id: str
    condition: Literal["New", "Used", "Collectible", "Refurbished"]
    listing_price: float        # Item price before shipping — USE THIS for comparison
    shipping_price: float
    landed_price: float         # listing_price + shipping_price — do NOT use for comparison
    points: int = 0             # Amazon loyalty points — ignore for repricing
    is_buy_box_winner: bool
    is_fulfilled_by_amazon: bool  # FBA vs FBM — relevant context for the AI

class AmazonListingItem(BaseModel):
    """Own listing from SP-API listings endpoint."""
    sku: str
    asin: str
    title: str
    current_price: float
    condition: str
    status: str                 # "ACTIVE", "INACTIVE", etc.
    marketplace_id: str


def map_to_my_product(item: AmazonListingItem) -> MyProduct:
    return MyProduct(
        platform_product_id=item.asin,
        platform_sku=item.sku,
        title=item.title,
        current_price=item.current_price,
        platform="amazon",
        metadata={"condition": item.condition, "marketplace_id": item.marketplace_id},
    )

def map_to_competitor_products(offers: list[AmazonOffer]) -> list[CompetitorProduct]:
    return [
        CompetitorProduct(
            price=offer.listing_price,        # listing_price only — not landed
            platform="amazon",
            is_fulfilled_by_platform=offer.is_fulfilled_by_amazon,
            extra={
                "is_buy_box_winner": offer.is_buy_box_winner,
                "condition": offer.condition,
                "seller_id": offer.seller_id,
            },
        )
        for offer in offers
        if offer.condition == "New"           # Only compare New condition by default
    ]
```

### 3.8 Buy Box Context for AI

Amazon's Buy Box (Featured Offer) is the primary purchase surface — whoever wins it gets ~85% of sales. Pass Buy Box context to the AI in every repricing request:

```python
# Added to the product context dict sent to Claude
"amazon_context": {
    "buy_box_winner_price": min(
        o.listing_price for o in offers if o.is_buy_box_winner
    ) if any(o.is_buy_box_winner for o in offers) else None,
    "seller_is_buy_box_winner": any(
        o.is_buy_box_winner and o.seller_id == self._credentials["merchant_id"]
        for o in offers
    ),
    "fba_competitor_count": sum(1 for o in offers if o.is_fulfilled_by_amazon),
    "fbm_competitor_count": sum(1 for o in offers if not o.is_fulfilled_by_amazon),
}
```

The AI uses this to reason about Buy Box eligibility vs margin tradeoffs.

### 3.9 Error Handling

| HTTP Status | Meaning | Connector Action |
|---|---|---|
| 400 on token refresh | Refresh token revoked/expired | Raise `PlatformAuthError` → user must reconnect |
| 403 | Insufficient permissions | Raise `PlatformAuthError` with message about missing role |
| 429 | Rate limit hit | Sleep `retry-after` header value, retry (tenacity) |
| 503 | Amazon API outage | Retry 3× with exponential backoff, then `FAILED` |
| 400 on price update | Invalid price format or SKU | Log ERROR, set job FAILED, do not retry |

### 3.10 SP-API App Setup — Founder Action Required

1. Go to [Seller Central Developer Console](https://sellercentral.amazon.com/sellercentral/developer/register)
2. Register as developer → create app → select "SP-API" → request roles listed in Section 3.4
3. Note the **Client ID** and **Client Secret** → add to `.env`:
   ```
   AMAZON_APP_CLIENT_ID=amzn1.application-oa2-client...
   AMAZON_APP_CLIENT_SECRET=...
   ```
4. Set OAuth redirect URI to: `https://pricebot.io/platforms/amazon/callback` (or `localhost:8000` for dev)
5. Amazon review process for production access typically takes 3–5 business days — submit early
6. For testing: use Sandbox environment (`sellingpartnerapi-na.amazon.com/sandbox`) with test credentials

**Marketplace IDs for reference:**
| Marketplace | ID |
|---|---|
| US | `ATVPDKIKX0DER` |
| Canada | `A2EUQ1WTGCTBG2` |
| UK | `A1F83G8C2ARO7P` |
| Germany | `A1PA6795UKMFR9` |
| Australia | `A39IBJ37TRP1C6` |

---

## 4. Etsy API (P1 — Build Second)

### 4.1 Auth Model
Etsy uses OAuth 2.0 with PKCE:
- Client ID: app-level (`ETSY_CLIENT_ID` env var)
- Per-user: `access_token` (1 hour) + `refresh_token` (90 days)
- Both stored encrypted in DB; access token refreshed at runtime when expired

```python
# platforms/etsy.py — credentials dict structure
{
    "access_token": "...",        # Short-lived, refresh when expired
    "refresh_token": "...",       # 90 days, encrypted in DB
    "shop_id": "12345678",        # Seller's Etsy shop ID
}
```

### 4.2 Key Endpoints

| Operation | Endpoint | Notes |
|---|---|---|
| Get own listings | `GET /v3/application/shops/{shop_id}/listings/active` | Paginated |
| Get listing prices | `GET /v3/application/listings/{listing_id}` | Own listing detail |
| Search competitor listings | `GET /v3/application/listings/active` | Search by keywords |
| Update listing price | `PATCH /v3/application/shops/{shop_id}/listings/{listing_id}` | Update `price` field |

### 4.3 Competitor Price Discovery — Etsy Specifics
Etsy has no direct "same item" competitor lookup (no ASIN equivalent). Competitor price discovery uses keyword search:
1. Extract product title keywords from the seller's listing
2. Search active listings with those keywords
3. Filter by same category
4. Return top 10 price points as competitor data

This is less precise than Amazon's ASIN-based lookup. The AI prompt must be told this context so it reasons appropriately about Etsy price comparison.

### 4.4 Rate Limits
- 10,000 API requests per day per app
- ~8 requests/second burst
- Etsy rate limits are per-app (not per-user) — track daily usage in DB

### 4.5 Etsy Price Format
Etsy prices are returned as objects: `{"amount": 1499, "divisor": 100, "currency_code": "USD"}`. Always convert: `price = amount / divisor`. Store and compare as `float`.

---

## 5. Shopify Admin API (P2 — Pending Validation)

> Do not implement until founder confirms Shopify validation threshold met.

### 5.1 Auth Model
- Custom apps: permanent access token per shop (simplest model for SaaS)
- OAuth flow: shop installs PriceBot → PriceBot receives permanent `access_token` scoped to that shop
- Scopes required: `read_products`, `write_products`, `read_price_rules`

### 5.2 Key Endpoints
- Own products: `GET /admin/api/2024-01/products.json`
- Update variant price: `PUT /admin/api/2024-01/variants/{variant_id}.json`
- Competitor prices: No native API — requires external scraping or price intelligence feed

### 5.3 Shopify Competitor Data Challenge
Shopify stores are independent — there is no marketplace-level competitor price API. Options:
1. Google Shopping API / SerpApi for competitor price data (adds cost per lookup)
2. Partner with a price intelligence provider
3. Limit Shopify competitor data to brands the seller manually specifies

This is a product decision. Flag to founder before implementing Shopify connector.

---

## 6. eBay API (P3 — Pending Validation)

> Do not implement until founder confirms eBay validation threshold met.

### 6.1 Auth Model
- OAuth 2.0 user tokens
- Scopes: `https://api.ebay.com/oauth/api_scope/sell.inventory`, `sell.marketing`
- User token (1 hour) + refresh token (18 months)

### 6.2 Key Endpoints
- Own listings: `GET /sell/inventory/v1/inventory_item`
- Competitor prices: `GET /buy/browse/v1/item_summary/search` (buyer API — different auth)
- Update price: `POST /sell/inventory/v1/offer/{offerId}/publish` after `PUT /sell/inventory/v1/offer/{offerId}`

### 6.3 eBay Specifics
- Every listing has a separate "offer" object with the price — updating price = updating the offer, then re-publishing
- eBay has both Fixed Price and Auction formats — only target Fixed Price listings

---

## 7. WooCommerce REST API (P4 — Pending Validation)

> Do not implement until founder confirms WooCommerce validation threshold met.

### 7.1 Auth Model
- Consumer Key + Consumer Secret (generated in WooCommerce admin)
- Basic Auth over HTTPS
- No OAuth flow — credentials are long-lived until regenerated

### 7.2 Key Endpoints
- Own products: `GET /wp-json/wc/v3/products`
- Update price: `PUT /wp-json/wc/v3/products/{id}` with `regular_price` field
- Variable products: price lives on variations — `PUT /wp-json/wc/v3/products/{id}/variations/{id}`

### 7.3 WooCommerce Competitor Data
Same challenge as Shopify — no marketplace competitor API. Requires external price intelligence. Flag to founder.

### 7.4 Connection Variability
Each WooCommerce store is a unique URL. Credential storage must include the shop's base URL:
```python
{
    "shop_url": "https://mystore.com",
    "consumer_key": "ck_...",
    "consumer_secret": "cs_...",
}
```

---

## 8. Platform Credentials Schema (DB)

```sql
CREATE TABLE platform_connections (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    platform        TEXT NOT NULL CHECK (platform IN (
                        'amazon', 'etsy', 'shopify', 'ebay', 'woocommerce'
                    )),
    encrypted_creds TEXT NOT NULL,          -- AES-256-GCM encrypted JSON blob
    shop_identifier TEXT,                   -- Human-readable: shop name or seller ID
    is_active       BOOLEAN DEFAULT TRUE,
    last_validated  TIMESTAMPTZ,
    invalidated_at  TIMESTAMPTZ,            -- Set when credentials fail validation
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (user_id, platform)              -- One connection per platform per user
);
```

**Never return `encrypted_creds` in any API response.** The column is read-only for the worker subsystem.

---

## 9. Adding a New Platform — Checklist

When adding a new platform connector:

- [ ] Implement all four abstract methods from `BasePlatformConnector`
- [ ] Write Pydantic models for platform-native data formats
- [ ] Map to `MyProduct` and `CompetitorProduct` internal models
- [ ] Implement token refresh logic if platform uses short-lived tokens
- [ ] Add platform to `CHECK` constraint in `platform_connections` table migration
- [ ] Add platform to `TIER_PLATFORM_LIMITS` in `docs/PRICING.md`
- [ ] Write unit tests with mocked API responses (fixtures in `tests/fixtures/`)
- [ ] Update `docs/PLATFORMS.md` with auth model and key endpoints
- [ ] Add platform connection UI in `frontend/app/dashboard/platforms/`