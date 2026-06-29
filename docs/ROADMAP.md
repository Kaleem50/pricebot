# ROADMAP.md — 8-Week Build Sequence

> This is the execution contract. The Week 8 launch date does not move. Perfectionism is channelled into the core engine and security model only — everything else ships functional and rough.

---

## 0. Guiding Principles

| Principle | Application |
|---|---|
| **Validate before building** | No platform connector code until signal threshold cleared |
| **Ship at 90%** | Functional + secure core. UI polish comes from user feedback |
| **One thing at a time** | Complete each phase before starting the next |
| **External accountability** | Beta users exist by Week 7. Their access is a hard deadline |
| **Cost-first architecture** | Batch API + prompt caching active before first real user |

---

## 1. What Is Done

| Item | Status |
|---|---|
| Core repricing engine (`core/repricing_engine.py`) | ✅ Built and tested |
| Demo runner (`demo.py`) | ✅ Working with sample data |
| Project structure (`core/`, `platforms/`, `workers/`, etc.) | ✅ Created |
| `requirements.txt` | ✅ Ready |
| `.env.example` | ✅ Ready |
| All `docs/*.md` files | ✅ Generated |
| Facebook validation posts (Amazon FBA, Etsy, Shopify, eBay) | ✅ Live |
| Market signals: 3 Amazon FBA + 1 Etsy pain confirmations | ✅ Threshold cleared |
| DB schema + RLS policies | ✅ Defined in `docs/DATABASE.md` |
| FastAPI skeleton + auth middleware | ✅ Defined in `docs/ARCHITECTURE.md` |

---

## 2. Week-by-Week Build Plan

---

### WEEK 1 — Foundation Layer ✅ → IN PROGRESS

**Goal:** Running FastAPI app connected to Supabase with auth working end-to-end.

**Tasks:**
- [ ] `db/migrations/001_initial_schema.sql` — full schema from `docs/DATABASE.md`
- [ ] `db/rls_policies.sql` — all RLS policies applied
- [ ] `db/client.py` — Supabase connection pool singleton
- [ ] `api/main.py` — FastAPI application skeleton with CORS, middleware mount
- [ ] `api/dependencies.py` — `get_db()`, `get_current_user()` shared dependencies
- [ ] `api/middleware/auth_guard.py` — JWT validation, `user_id` extraction, tier check
- [ ] `api/middleware/rate_limiter.py` — token-bucket rate limiter
- [ ] `api/routers/auth.py` — register, login, refresh (delegates to Supabase Auth)
- [ ] `Makefile` — `make dev`, `make test`, `make migrate` commands
- [ ] Verify: `curl localhost:8000/health` returns 200; auth flow works end-to-end

**Success criteria:** A new user can register, log in, receive a JWT, and hit a protected route.

---

### WEEK 2 — Amazon Connector + Billing  ← ACTIVE

**Goal:** PriceBot can connect to a real Amazon seller account, pull products, fetch competitor prices, and bill via Stripe.

**Tasks — Foundation:**
- [ ] `core/crypto.py` — AES-256-GCM `encrypt_credential()` / `decrypt_credential()` utility
- [ ] `platforms/base.py` — abstract `BasePlatformConnector` with full typed contract
- [ ] `platforms/exceptions.py` — `PlatformAuthError`, `PlatformRateLimitError`, `PlatformProductNotFoundError`, `PlatformAPIError`

**Tasks — Amazon Connector (`platforms/amazon.py`):**
- [ ] OAuth redirect handler: build auth URL, validate CSRF state on callback, exchange code for tokens
- [ ] `_get_access_token()` — LWA token exchange with 1-hour in-memory cache
- [ ] `validate_credentials()` — test token against a lightweight SP-API call
- [ ] `get_products()` — paginated listings fetch, map to `MyProduct` models
- [ ] `get_competitor_prices()` — batched ASIN lookup (20 per call), rate-limit sleep, map to `CompetitorProduct`
- [ ] `apply_price()` — PATCH listings endpoint with `purchasable_offer`, retry on 429
- [ ] Buy Box context builder — extract `buy_box_winner_price`, `seller_is_winner`, FBA vs FBM counts
- [ ] Token-bucket rate limiter at connector level (1 req/sec for pricing, 5 req/sec for listings)

**Tasks — API Layer:**
- [ ] `api/routers/platforms.py` — connect (store encrypted creds), disconnect, sync, list endpoints
- [ ] `api/routers/products.py` — paginated product list, product detail, update margin floor
- [ ] `api/routers/billing.py` — subscription status, Stripe portal session, webhook handler

**Tasks — Billing:**
- [ ] Create Stripe products + price objects (founder action — Starter $9, Growth $29, Pro $59)
- [ ] Add price IDs to `.env`
- [ ] Stripe webhook: handle `subscription.created`, `subscription.updated`, `subscription.deleted`, `invoice.payment_failed`

**Tasks — Tests:**
- [ ] `tests/fixtures/amazon_responses.py` — mocked SP-API response fixtures
- [ ] `tests/unit/test_amazon_connector.py` — all four methods with mocked HTTP
- [ ] `tests/unit/test_crypto.py` — encrypt/decrypt round-trip, tampered ciphertext raises
- [ ] `tests/unit/test_billing_webhook.py` — HMAC validation, each event type

**Verify:**
- [ ] Founder connects their own Amazon Seller account through the API → products appear in DB
- [ ] Competitor prices fetched for 3 test ASINs → correct shape in response
- [ ] Stripe test webhook processed → `subscriptions` row updated in DB
- [ ] Tampered encrypted credential raises `InvalidTag` on decrypt attempt

**Success criteria:** Amazon credentials stored encrypted, products imported, competitor prices fetched, Stripe subscription webhook processed. All unit tests pass.

---

### WEEK 3 — Worker Pipeline

**Goal:** Full repricing cycle running end-to-end with real Claude Batch API calls.

**Tasks:**
- [ ] `workers/batch_submitter.py` — collect IDLE jobs, build batch requests, submit to Anthropic
- [ ] `workers/batch_poller.py` — poll batch status, parse results, trigger price applicator
- [ ] `workers/scheduler.py` — APScheduler setup, 15-min submission cycle, 5-min poll cycle, 1-hr recovery
- [ ] Price applicator logic within `batch_poller.py` — fail-safe guardrail, state transitions, `price_history` writes
- [ ] `db/migrations/002_add_usage_events.sql` — usage tracking table
- [ ] Cost monitoring: log token usage, cache hit rate, estimated cost per batch
- [ ] Stale job recovery job (hourly)
- [ ] Verify: Run full cycle with test Amazon products → see `price_history` rows written, correct state transitions

**Success criteria:** End-to-end cycle completes. Jobs transition IDLE → BATCH_SUBMITTED → SYNCED. Fail-safe guardrail tested with a deliberately invalid Claude response.

---

### WEEK 4 — Dashboard MVP

**Goal:** A non-technical seller can log in and understand exactly what PriceBot is doing.

**Tasks:**
- [ ] Next.js 14 project scaffold in `frontend/`
- [ ] `/login` and `/register` pages with Supabase Auth integration
- [ ] `/dashboard` — overview: product count, reprices today, estimated savings
- [ ] `/dashboard/products` — product table: title, current price, suggested price, state badge, last updated
- [ ] `PriceSuggestionCard` component — current price → suggested price + reasoning sentence + confidence badge
- [ ] Platform connection wizard (Amazon only for now) — 4-step stepper
- [ ] `/dashboard/billing` — current plan, product usage, upgrade CTA
- [ ] Empty states: clear CTAs on every page with no data ("Connect your first store →")
- [ ] Verify: Walk a non-technical person through the UI — they understand every element without explanation

**Success criteria:** A seller can register, connect their Amazon store, see their products, and understand a price suggestion — without asking a single question.

---

### WEEK 5 — Etsy Connector + Beta Prep

**Goal:** Second platform live. Three beta users lined up with access ready to go.

**Tasks:**
- [ ] `platforms/etsy.py` — Etsy API connector (OAuth PKCE flow, keyword-based competitor search)
- [ ] Add Etsy to platform connection wizard
- [ ] Etsy-specific product sync and competitor data mapping
- [ ] `/dashboard/history` — full price change log with AI reasoning
- [ ] `/dashboard/settings` — margin floor defaults, notification email preferences
- [ ] Beta user onboarding checklist: Loom walkthrough video script prepared
- [ ] Email notification on price change (transactional email via Resend or Postmark)
- [ ] Identify 3 beta users from Facebook validation respondents (founder action)
- [ ] Verify: Etsy full cycle works end-to-end; email notification arrives after price change

**Success criteria:** Two platforms operational. Three beta users identified and pre-briefed.

---

### WEEK 6 — Beta Access + Hardening

**Goal:** Beta users have access. Real usage exposes real bugs.

**Tasks:**
- [ ] Provision beta user accounts (free access, 2-week window)
- [ ] Send beta access + Loom walkthrough to 3 users (founder action)
- [ ] Monitor error logs daily — fix any CRITICAL or ERROR logs same day
- [ ] Monitor batch job failure rate — target <5%
- [ ] Fix top 3 issues surfaced by beta users
- [ ] Supabase performance: review slow query log, add any missing indexes
- [ ] Security audit: run pre-deployment checklist from `docs/SECURITY.md`
- [ ] `/dashboard/products/[id]` — product detail page with price history chart
- [ ] Stripe billing portal working end-to-end (upgrade/downgrade self-serve)

**Success criteria:** All 3 beta users have made at least one repricing cycle successfully. No CRITICAL-level errors in 48-hour window.

---

### WEEK 7 — Polish + Launch Prep

**Goal:** Product is launch-ready. One week of buffer before public launch.

**Tasks:**
- [ ] Collect structured feedback from beta users (3 questions: what worked, what confused you, would you pay $29/month)
- [ ] Implement top 2–3 changes from beta feedback
- [ ] Mobile-responsive dashboard review — at minimum, the product list must be usable on mobile
- [ ] Error boundary components in frontend — user-friendly error states (no raw stack traces)
- [ ] Onboarding improvement based on beta confusion points
- [ ] Write ProductHunt post draft (founder action)
- [ ] Write Reddit posts for r/FulfillmentByAmazon, r/EtsySellers (founder action)
- [ ] Write IndieHackers launch post (founder action)
- [ ] Final security audit against `docs/SECURITY.md` checklist
- [ ] Load test: simulate 10 concurrent users running reprice cycles

**Success criteria:** Beta feedback incorporated. Launch posts ready. Product handles 10 concurrent users without degradation.

---

### WEEK 8 — Launch

**Goal:** First paying user acquired.

**Tasks (Day 1):**
- [ ] Post on ProductHunt
- [ ] Post in r/FulfillmentByAmazon and r/EtsySellers
- [ ] Publish IndieHackers post with build story
- [ ] Monitor Stripe dashboard every hour

**Tasks (ongoing):**
- [ ] Reply to every comment and DM personally (founder action)
- [ ] Convert beta users to paid — offer 1-month discount as beta appreciation
- [ ] Watch for patterns in free-to-paid conversion blockers
- [ ] Log every user conversation insight in a decision log

**Success criteria:** **One paying user at any tier.** This is the only metric that matters this week.

---

## 3. Post-Launch Priorities (Week 9+)

These are ordered by impact, not complexity. Build what the first 10 users ask for most.

| Priority | Feature | Rationale |
|---|---|---|
| P0 | Shopify connector | If validation signals clear — second-largest market |
| P0 | eBay connector | If validation signals clear |
| P1 | Overage billing (Pro) | Required before Pro users with large catalogs sign up |
| P1 | API access (Pro) | Unlocks power users who want to build on top of PriceBot |
| P2 | Price war detection | Alert seller when competitor enters a race-to-the-bottom pattern |
| P2 | Demand spike pricing | Increase price when demand signals are high (BSR movement, trending) |
| P3 | WooCommerce connector | Pending validation |
| P3 | Mobile app (iOS/Android) | Only if sellers request it — dashboard is mobile-responsive already |

---

## 4. North Star Metrics

| Metric | Target | Timeframe |
|---|---|---|
| First paying user | 1 | Week 8 |
| Monthly profit | $1,000 | Month 3 |
| Monthly profit | $2,100 | Month 6 |
| Monthly profit | $10,500 | Month 12 |
| Batch job success rate | >95% | Always |
| Price guardrail triggers | <1% of jobs | Always |
| Dashboard time-to-first-reprice | <15 min from signup | Always |

---

## 5. What Does Not Move

These constraints are fixed regardless of pressure, feedback, or perfectionism:

| Fixed Constraint | Reason |
|---|---|
| Week 8 public launch date | External accountability beats internal motivation |
| Validate before building new platforms | Building unvalidated connectors wastes weeks |
| Batch API + prompt caching before first user | Margin integrity |
| Fail-safe guardrail in every price-write path | Trust and legal safety |
| One paying user as Week 8 success criterion | Proof of model, not perfection of product |