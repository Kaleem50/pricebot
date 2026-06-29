# SECURITY.md — PriceBot Security Model

> Every feature built must be reviewed against this document before completion. Security is not a post-launch concern — it is a pre-condition of shipping.

---

## 1. Threat Model

PriceBot handles three categories of sensitive data:

| Data Category | Sensitivity | Attack Impact if Leaked |
|---|---|---|
| Platform API credentials (Amazon SP-API keys, Shopify tokens, etc.) | **Critical** | Full takeover of seller's store — price changes, inventory access, orders |
| Margin floors and cost data | **High** | Competitor intelligence — exposes seller's actual costs |
| Pricing history and strategy | **Medium** | Competitive intelligence |
| User PII (email, name) | **Medium** | Spam, phishing, identity exposure |

### 1.1 Primary Threat Vectors

| Threat | Vector | Mitigation |
|---|---|---|
| Cross-tenant data leak | Missing `user_id` filter in SQL query | RLS policies + application-layer filter |
| Credential theft | Platform keys stored in plaintext | AES-256-GCM encryption at rest |
| Unsafe price applied | Claude returns bad/null price | Fail-safe guardrail in applicator |
| Budget exhaustion | Malicious/looping API abuse | Token-bucket rate limiting |
| Privilege escalation | Client supplies own tier value | Tier always read from Stripe record in DB |
| JWT forgery | Fake tokens sent to API | Supabase JWT verification with secret |
| Webhook replay | Stripe webhook replayed | HMAC signature verification + timestamp check |
| Credential rotation gap | Old credentials used after re-auth | Platform connections table has `invalidated_at` |

---

## 2. Authentication & Authorization

### 2.1 JWT Flow
- Supabase Auth issues JWTs on login
- Every protected route validates the JWT via `auth_guard.py` using Supabase's public key
- `user_id` is extracted from validated token claims — **never from request body or query params**
- Token expiry: access token 1 hour, refresh token 7 days
- Refresh tokens are rotated on each use

### 2.2 auth_guard.py Contract

```python
async def get_current_user(
    authorization: str = Header(...),
    db: Client = Depends(get_db),
) -> AuthenticatedUser:
    """
    Validate JWT, extract user_id, verify subscription active.
    Raises HTTP 401 if token invalid or expired.
    Raises HTTP 403 if subscription inactive.
    """
    token = authorization.removeprefix("Bearer ")
    payload = verify_jwt(token)  # raises on invalid/expired
    user_id = payload["sub"]
    subscription = get_subscription(db, user_id)
    if not subscription.is_active:
        raise HTTPException(status_code=403, detail="Subscription inactive")
    return AuthenticatedUser(id=user_id, tier=subscription.tier)
```

### 2.3 Tier Enforcement
Tier is read from the `subscriptions` table, which is updated exclusively by the Stripe webhook handler. Client-supplied tier values are never trusted.

```python
# auth_guard.py — tier enforcement on protected routes
def require_tier(minimum_tier: Tier):
    def decorator(current_user: AuthenticatedUser = Depends(get_current_user)):
        if current_user.tier < minimum_tier:
            raise HTTPException(
                status_code=403,
                detail=f"This feature requires {minimum_tier.name} plan or higher."
            )
    return decorator

# Usage
@router.post("/products/{id}/auto-reprice")
async def enable_auto_reprice(
    _: None = Depends(require_tier(Tier.GROWTH)),  # Starter cannot auto-reprice
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    ...
```

---

## 3. Platform Credential Security

### 3.1 Encryption Spec
- Algorithm: AES-256-GCM (authenticated encryption — provides both confidentiality and integrity)
- Key: 256-bit key stored in `CREDENTIAL_ENCRYPTION_KEY` environment variable
- Storage: Key lives **only** in the environment — never in Supabase, never in logs
- IV: Randomly generated per encryption operation (stored alongside ciphertext)
- Auth tag: Stored alongside ciphertext (GCM provides integrity verification on decrypt)

### 3.2 Encryption Implementation

```python
# core/crypto.py
import os
import base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

def encrypt_credential(plaintext: str, key_hex: str) -> str:
    """
    Encrypt a platform API credential for DB storage.
    Returns base64-encoded: nonce (12 bytes) + ciphertext + auth_tag.
    """
    key = bytes.fromhex(key_hex)
    nonce = os.urandom(12)  # 96-bit nonce — never reuse
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return base64.b64encode(nonce + ciphertext).decode()

def decrypt_credential(encrypted_b64: str, key_hex: str) -> str:
    """
    Decrypt a platform API credential retrieved from DB.
    Raises InvalidTag if ciphertext has been tampered with.
    """
    key = bytes.fromhex(key_hex)
    data = base64.b64decode(encrypted_b64)
    nonce, ciphertext = data[:12], data[12:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, None).decode()
```

### 3.3 Credential Lifecycle
```
User submits credentials via dashboard
  → POST /platforms/{platform}/connect
  → Validated against platform API (validate_credentials())
  → Encrypted with AESGCM
  → Stored in platform_connections table (encrypted_credentials column)
  → Plaintext never persisted, never logged

Worker needs credentials for a product
  → Reads encrypted_credentials from platform_connections
  → Decrypts in-memory at job start
  → Passes decrypted dict to connector instance
  → Connector instance scope ends → decrypted credentials GC'd

User disconnects platform
  → DELETE /platforms/{platform}
  → platform_connections row deleted (or invalidated_at set)
  → All active jobs for that platform set to FAILED
```

### 3.4 What Must Never Happen
- Plaintext credentials in application logs at any log level
- Plaintext credentials in error messages returned to client
- `encrypted_credentials` column returned in any API response
- Decrypted credentials stored in Redis, session, or any persistent cache
- Key hardcoded or committed to repository

---

## 4. Row-Level Security (RLS)

### 4.1 Policy — Every Table

```sql
-- products table
ALTER TABLE products ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users access own products only"
ON products
FOR ALL
USING (user_id = auth.uid());

-- platform_connections table
ALTER TABLE platform_connections ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users access own connections only"
ON platform_connections
FOR ALL
USING (user_id = auth.uid());

-- repricing_jobs table
ALTER TABLE repricing_jobs ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users access own jobs only"
ON repricing_jobs
FOR ALL
USING (user_id = auth.uid());

-- price_history table
ALTER TABLE price_history ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users access own price history only"
ON price_history
FOR ALL
USING (user_id = auth.uid());
```

### 4.2 Service Role Usage (Workers)
Background workers use `SUPABASE_SERVICE_ROLE_KEY` which bypasses RLS. This is required for the scheduler to process all users. Because RLS is bypassed, workers must **explicitly filter by user_id in every query** — this is not optional.

```python
# Worker query — must include user_id filter even with service role
jobs = db.table("repricing_jobs") \
    .select("*") \
    .eq("user_id", user_id) \       # MANDATORY even with service role
    .eq("state", "IDLE") \
    .execute()
```

### 4.3 New Table Checklist
When adding any new table to the schema:
1. Write the RLS policy in `db/rls_policies.sql` before the table is used
2. `ALTER TABLE <name> ENABLE ROW LEVEL SECURITY;` must be in the migration
3. Policy must cover SELECT, INSERT, UPDATE, DELETE separately if behaviors differ
4. Workers using service role must add explicit `user_id` filter

---

## 5. Rate Limiting

### 5.1 Token Bucket Configuration

```python
# api/middleware/rate_limiter.py
RATE_LIMITS = {
    Tier.STARTER: RateLimit(requests_per_minute=30, batch_submits_per_hour=5),
    Tier.GROWTH: RateLimit(requests_per_minute=60, batch_submits_per_hour=10),
    Tier.PRO: RateLimit(requests_per_minute=120, batch_submits_per_hour=20),
}
```

Rate limit state is stored in Supabase (simple counter table) to survive worker restarts.

### 5.2 Batch Submission Guard
Before the scheduler submits any batch to Anthropic, it enforces:

```python
def can_submit_batch(user_id: str, tier: Tier, product_count: int) -> bool:
    """
    Validate all submission constraints before touching Anthropic API.
    Returns False (with logged reason) if any constraint fails.
    """
    tier_limits = TIER_LIMITS[tier]

    # 1. Product count within tier cap
    if product_count > tier_limits.max_products:
        logger.warning("Batch rejected — product count exceeds tier cap", extra={...})
        return False

    # 2. Reprice frequency within daily cap
    today_reprice_count = get_today_reprice_count(user_id)
    if today_reprice_count >= tier_limits.max_daily_reprices:
        logger.info("Batch skipped — daily frequency cap reached", extra={...})
        return False

    # 3. Hourly batch submission rate
    hour_submit_count = get_hour_batch_count(user_id)
    if hour_submit_count >= RATE_LIMITS[tier].batch_submits_per_hour:
        logger.warning("Batch rejected — hourly submission rate exceeded", extra={...})
        return False

    return True
```

---

## 6. Fail-Safe Repricing Guardrail

This is the most business-critical security control in the entire system. An unsafe price applied to a seller's listing can cause immediate financial harm.

### 6.1 The Guardrail

```python
def apply_repricing_result(
    product: Product,
    claude_result: RepricingRecommendation | None,
    db: Client,
    connector: BasePlatformConnector,
) -> None:
    """
    Apply a repricing result with mandatory fail-safe guardrail.
    This function is the only place prices are written to platforms.
    """
    # Guard 1: Claude must have returned a valid result
    if claude_result is None:
        logger.critical("Guardrail: Claude returned null result — aborting", extra={
            "product_id": product.id,
            "user_id": product.user_id,
        })
        set_job_state(db, product.id, "FAILED", reason="claude_null_response")
        return

    # Guard 2: Recommended price must be a positive number
    if not isinstance(claude_result.recommended_price, (int, float)) \
            or claude_result.recommended_price <= 0:
        logger.critical("Guardrail: Claude returned invalid price — aborting", extra={
            "product_id": product.id,
            "claude_price": claude_result.recommended_price,
        })
        set_job_state(db, product.id, "FAILED", reason="claude_invalid_price")
        return

    # Guard 3: Enforce minimum safe price — this line is never removed
    floor_price = product.cost + product.min_margin_floor
    final_price = max(claude_result.recommended_price, floor_price)

    if final_price != claude_result.recommended_price:
        logger.warning("Guardrail: floor price applied", extra={
            "product_id": product.id,
            "claude_price": claude_result.recommended_price,
            "floor_price": floor_price,
            "final_price": final_price,
        })

    # Guard 4: Final price must not exceed a reasonable ceiling (10× current price)
    ceiling_price = product.current_price * 10
    if final_price > ceiling_price:
        logger.critical("Guardrail: price exceeds ceiling — aborting", extra={
            "product_id": product.id,
            "final_price": final_price,
            "ceiling": ceiling_price,
        })
        set_job_state(db, product.id, "FAILED", reason="price_exceeds_ceiling")
        return

    # All guards passed — safe to apply
    connector.apply_price(product, final_price)
```

### 6.2 What Triggers CRITICAL Logs
Any CRITICAL log must be surfaced to the operator. In production, wire CRITICAL logs to an alert channel (email or Slack webhook).

| Trigger | Log Message |
|---|---|
| Claude returns null | `"Guardrail: Claude returned null result — aborting"` |
| Claude returns invalid price | `"Guardrail: Claude returned invalid price — aborting"` |
| Price exceeds ceiling | `"Guardrail: price exceeds ceiling — aborting"` |
| Platform credential decryption fails | `"Credential decryption failed — connector cannot start"` |
| Stripe webhook HMAC mismatch | `"Stripe webhook signature verification failed"` |
| Unauthorized cross-tenant access attempt | `"Tenant isolation violation detected"` |

---

## 7. Stripe Webhook Security

```python
@router.post("/billing/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("Stripe-Signature")

    # HMAC verification — reject anything that doesn't pass
    try:
        event = stripe.Webhook.construct_event(
            payload,
            sig_header,
            settings.STRIPE_WEBHOOK_SECRET,
        )
    except stripe.error.SignatureVerificationError:
        logger.critical("Stripe webhook signature verification failed", extra={
            "sig_header": sig_header[:20] + "...",  # log partial only
        })
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Handle event
    handle_stripe_event(event)
```

---

## 8. Secret Management

### 8.1 Environment Variables — Never in Code

```bash
# .env — never committed to git
ANTHROPIC_API_KEY=sk-ant-...
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJ...
SUPABASE_ANON_KEY=eyJ...
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
CREDENTIAL_ENCRYPTION_KEY=<64-char hex — generate: openssl rand -hex 32>
JWT_SECRET=<random — generate: openssl rand -hex 32>
```

### 8.2 .gitignore — Mandatory Entries
```
.env
.env.local
.env.production
*.pem
*.key
```

### 8.3 Encryption Key Rotation
If `CREDENTIAL_ENCRYPTION_KEY` must be rotated:
1. Add `CREDENTIAL_ENCRYPTION_KEY_OLD` to environment
2. Run migration script: decrypt all stored credentials with old key, re-encrypt with new key
3. Remove `CREDENTIAL_ENCRYPTION_KEY_OLD` after migration verified
4. Script lives in `db/migrations/scripts/rotate_credentials.py`

---

## 9. Security Review — Required Before Every Deployment

- [ ] No secrets in committed code (`git grep -i "sk-ant\|sk_live\|eyJ" -- '*.py' '*.ts'` returns nothing)
- [ ] All new Supabase tables have RLS enabled and policies defined
- [ ] All new API endpoints have `get_current_user` dependency
- [ ] All new DB queries in workers have explicit `user_id` filter
- [ ] No `encrypted_credentials` field returned in any API response schema
- [ ] Stripe webhook endpoint verifies HMAC signature before processing
- [ ] Fail-safe guardrail present in price applicator
- [ ] No platform credentials appear at any log level
- [ ] Rate limiting applied to all new routes
- [ ] New tier-gated features use `require_tier()` dependency