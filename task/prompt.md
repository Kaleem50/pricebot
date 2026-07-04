Read CLAUDE.md completely. Then read docs/PLATFORMS.md §4 (Etsy API spec)
and docs/SECURITY.md before writing a single line of code.

Current project state:
- Weeks 1-4 complete, 165/165 tests passing
- platforms/amazon.py — complete, do not touch
- platforms/mock.py — complete, do not touch
- All docs/*.md — complete, do not touch

Current task: Build Etsy connector end-to-end and verify full
repricing cycle works with a real Etsy account.

Build in this exact order:

1. platforms/etsy.py — Full BasePlatformConnector implementation

   credentials dict stored encrypted in DB:
   {
     "access_token": "...",   # short-lived 1hr
     "refresh_token": "...",  # 90 days
     "shop_id": "12345678",   # seller's Etsy shop ID
   }

   _refresh_access_token():
   - POST https://api.etsy.com/v3/public/oauth/token
   - grant_type=refresh_token
   - client_id from ETSY_CLIENT_ID env var
   - client_secret from ETSY_CLIENT_SECRET env var
   - Updates self._credentials["access_token"] in memory only
   - Raises PlatformAuthError on 400/401
   - Never logs the token value at any log level

   validate_credentials():
   - GET https://openapi.etsy.com/v3/application/users/me
   - Headers: x-api-key: {ETSY_CLIENT_ID}, Authorization: Bearer {token}
   - Returns True on 200
   - Raises PlatformAuthError on 401/403

   get_products():
   - GET https://openapi.etsy.com/v3/application/shops/{shop_id}/listings/active
   - Paginate: limit=100, offset increments until results array empty
   - Price conversion: price = listing.price.amount / listing.price.divisor
     This is CRITICAL — Etsy returns {"amount": 2499, "divisor": 100}
     which means $24.99. Never skip this conversion.
   - Filter: only state="active" listings
   - Map to MyProduct:
       platform_product_id = str(listing_id)
       platform_sku = None (Etsy has no SKU concept)
       title = title
       current_price = converted price
       platform = "etsy"

   get_competitor_prices():
   - Etsy has no direct ASIN-style competitor lookup
   - Strategy: keyword search using first 5 words of product title
   - GET https://openapi.etsy.com/v3/application/listings/active
       ?keywords={first_5_words}
       &limit=20
       &sort_on=price
       &sort_order=ascending
   - Filter out seller's own listings (match by shop_id in url field)
   - Map top 10 results to CompetitorProduct:
       price = amount / divisor (same conversion as above)
       condition = "new"
       is_fulfilled_by_platform = False
       extra = {"search_method": "keyword", "match_type": "approximate"}
   - Add {"search_method": "keyword"} to product platform_context
     so Claude knows data is approximate not exact-match
   - Rate limit: track daily request count in usage_events
     Raise PlatformRateLimitError if daily count > 9500
     (Etsy allows 10,000/day per app across ALL users)

   apply_price():
   - PATCH https://openapi.etsy.com/v3/application/shops/{shop_id}/listings/{listing_id}
   - Headers: x-api-key + Bearer token + Content-Type: application/json
   - Body: {"price": round(new_price, 2)}
   - Returns True on 200/204
   - Raises PlatformAPIError on any non-2xx

   Auto-refresh pattern (apply to all API calls):
   - On 401 response: call _refresh_access_token() once, retry request
   - On second 401: raise PlatformAuthError (refresh token also expired)
   - On 429: raise PlatformRateLimitError (tenacity handles retry)

2. Add to .env and .env.example:
   ETSY_CLIENT_ID=
   ETSY_CLIENT_SECRET=

3. Update platforms/__init__.py get_connector():
   - Add "etsy" case returning EtsyConnector
   - Keep all existing cases unchanged

4. Update api/routers/platforms.py:
   - Confirm "etsy" is in the allowed platform enum
   - Etsy connect flow: store encrypted credentials after validation
   - No changes to Amazon flow

5. Update frontend/components/platforms/ConnectWizard.tsx:
   - Remove "Coming soon" from Etsy
   - Etsy step 2 instructions (plain English):
     "Go to etsy.com/developers → Create a new app → copy your
      Keystring (Client ID) and OAuth token. You will need to
      authorize PriceBot to access your shop in the next step."
   - Etsy credential fields:
       Access Token (label: "OAuth Access Token")
       Refresh Token (label: "OAuth Refresh Token")
       Shop ID (label: "Your Etsy Shop ID — found in your shop URL")
   - Amazon credential fields remain unchanged

6. Update scripts/seed_test_products.py:
   - Add option --platform etsy to seed 2 Etsy test products
   - Etsy products use listing_id format (numeric string) not ASIN
   - Add a matching platform_connections row for etsy

7. tests/unit/test_etsy_connector.py — full coverage:
   - validate_credentials: success (200) and failure (401)
   - get_products: single page, two pages pagination
   - get_products: price conversion (amount=2499, divisor=100 → 24.99)
   - get_competitor_prices: keyword search, filters own shop correctly
   - get_competitor_prices: daily rate limit raises PlatformRateLimitError
   - apply_price: success (200) and API error (400)
   - Auto-refresh: 401 triggers token refresh then retries
   - Second 401 after refresh raises PlatformAuthError

8. End-to-end mock test:
   - Seed 2 Etsy products with mock credentials
   - Set MOCK_PLATFORM_MODE=true
   - Trigger repricing cycle
   - Confirm both products go IDLE → BATCH_SUBMITTED → PROCESSING → SYNCED
   - Confirm price_history written with search_method=keyword in context
   - Confirm 165+ existing tests still pass

Security checklist:
- ETSY_CLIENT_ID and ETSY_CLIENT_SECRET never logged
- Access token never returned in any API response
- encrypted_creds column never in any response
- Daily rate limit enforced before any Etsy API call
- All DB queries filter by user_id

Output the session snapshot when done.