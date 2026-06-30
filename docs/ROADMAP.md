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

**Last verified:** 2026-07-01, via live QA pass + full pytest run. Critical singleton bug found and fixed same session — 165/165 passing post-fix. See per-week sections below for detail.

**Overall: Weeks 1–4 complete and verified. Week 5 half-done (dashboard side only). Weeks 6–8 not started.**

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
| DB schema + RLS policies | ✅ Built — `db/migrations/001_initial_schema.sql`, `002_competitor_price_cache.sql`, `db/rls_policies.sql` |
| FastAPI skeleton + auth middleware | ✅ Built and live-tested — `api/main.py`, `api/dependencies.py`, `api/middleware/` |
| Week 1 — Foundation Layer | ✅ Complete |
| Week 2 — Amazon connector + billing code | ✅ Complete — Stripe account setup still blocked on founder |
| Week 3 — Worker pipeline (batch submit/poll/scheduler/recovery) | ✅ Complete |
| Week 4 — Dashboard MVP (+ dark mode, password toggle) | ✅ Complete |
| Week 5 — Etsy connector | ❌ Not built (empty stub file) |
| Week 5 — `/dashboard/history`, `/dashboard/settings`, repricing history API | ✅ Complete |
| Week 5 — Email notifications, beta user list | ❌ Not started |
| Week 6–8 | ⛔ Not started |
| **Critical bug: shared DB-client singleton contaminated by login/register/refresh** | ✅ **Fixed 2026-07-01** — `get_auth_client()` added, auth.py uses `get_auth_db()`, regression test in `tests/integration/` |

---

## 2. Week-by-Week Build Plan

---

### WEEK 1 — Foundation Layer ✅ COMPLETE

**Goal:** Running FastAPI app connected to Supabase with auth working end-to-end.

**Tasks:**
- [x] `db/migrations/001_initial_schema.sql` — full schema from `docs/DATABASE.md`
- [x] `db/rls_policies.sql` — all RLS policies applied
- [x] `db/client.py` — Supabase connection pool singleton
- [x] `api/main.py` — FastAPI application skeleton with CORS, middleware mount
- [x] `api/dependencies.py` — `get_db()`, `get_current_user()` shared dependencies
- [x] `api/middleware/auth_guard.py` — JWT validation, `user_id` extraction, tier check
- [x] `api/middleware/rate_limiter.py` — token-bucket rate limiter
- [x] `api/routers/auth.py` — register, login, refresh (delegates to Supabase Auth)
- [x] `Makefile` — `make dev`, `make test`, `make migrate` commands
- [x] Verify: `curl localhost:8000/health` returns 200; auth flow works end-to-end — confirmed via live QA pass 2026-07-01

**Success criteria:** ✅ MET — A new user can register, log in, receive a JWT, and hit a protected route. Verified live: register → 201, login → JWT, malformed/missing/expired token → 401, protected routes correctly JWT-gated.

---

### WEEK 2 — Amazon Connector + Billing  ✅ CODE COMPLETE — ⚠️ blocked on founder Stripe setup

**Goal:** PriceBot can connect to a real Amazon seller account, pull products, fetch competitor prices, and bill via Stripe.

**Tasks — Foundation:**
- [x] `core/crypto.py` — AES-256-GCM `encrypt_credential()` / `decrypt_credential()` utility
- [x] `platforms/base.py` — abstract `BasePlatformConnector` with full typed contract
- [x] `platforms/exceptions.py` — `PlatformAuthError`, `PlatformRateLimitError`, `PlatformProductNotFoundError`, `PlatformAPIError`

**Tasks — Amazon Connector (`platforms/amazon.py`):** — 880 lines, fully implemented
- [x] OAuth redirect handler: build auth URL, validate CSRF state on callback, exchange code for tokens
- [x] `_get_access_token()` — LWA token exchange with 1-hour in-memory cache
- [x] `validate_credentials()` — test token against a lightweight SP-API call
- [x] `get_products()` — paginated listings fetch, map to `MyProduct` models
- [x] `get_competitor_prices()` — batched ASIN lookup (20 per call), rate-limit sleep, map to `CompetitorProduct`
- [x] `apply_price()` — PATCH listings endpoint with `purchasable_offer`, retry on 429
- [x] Buy Box context builder — extract `buy_box_winner_price`, `seller_is_winner`, FBA vs FBM counts
- [x] Token-bucket rate limiter at connector level (1 req/sec for pricing, 5 req/sec for listings)

**Tasks — API Layer:**
- [x] `api/routers/platforms.py` — connect (store encrypted creds), disconnect, sync, list endpoints
- [x] `api/routers/products.py` — paginated product list, product detail, update margin floor
- [x] `api/routers/billing.py` — subscription status, Stripe portal session, webhook handler

**Tasks — Billing:**
- [ ] ⚠️ **BLOCKED (founder action)** — Create Stripe products + price objects (Starter $9, Growth $29, Pro $59)
- [ ] ⚠️ **BLOCKED (founder action)** — Add price IDs to `.env` — `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_*_PRICE_ID` are all still blank
- [x] Stripe webhook: handle `subscription.created`, `subscription.updated`, `subscription.deleted`, `invoice.payment_failed`, `invoice.payment_succeeded` — code complete, idempotent on `stripe_sub_id`

**Tasks — Tests:**
- [x] `tests/fixtures/amazon_responses.py` — mocked SP-API response fixtures
- [x] `tests/unit/test_amazon_connector.py` — all four methods with mocked HTTP
- [x] `tests/unit/test_crypto.py` — encrypt/decrypt round-trip, tampered ciphertext raises
- [x] `tests/unit/test_billing_webhook.py` — HMAC validation, each event type (mocked — see live-verify gap below)

**Verify:**
- [ ] Founder connects their own Amazon Seller account through the API → products appear in DB *(founder action — code path tested with mocks, not yet exercised against real SP-API)*
- [ ] Competitor prices fetched for 3 test ASINs → correct shape in response *(same — needs real Amazon sandbox/seller creds)*
- [ ] ⚠️ Stripe test webhook processed → `subscriptions` row updated in DB — **cannot verify live**, `STRIPE_WEBHOOK_SECRET` unset. Live QA pass 2026-07-01 confirmed the missing-config path returns a clean 500 "Webhook service not configured" with no DB write — correct behavior, but the actual HMAC-valid/invalid signature branches remain unexercised.
- [x] Tampered encrypted credential raises `InvalidTag` on decrypt attempt — covered in `test_crypto.py`

**Success criteria:** Amazon credentials stored encrypted ✅, products imported ✅ (connector built, untested against live SP-API), competitor prices fetched ✅ (same caveat), Stripe subscription webhook processed ⚠️ (code complete, blocked on founder Stripe account setup). All unit tests pass ✅ — 163/163 as of 2026-07-01.

---

### WEEK 3 — Worker Pipeline ✅ COMPLETE

**Goal:** Full repricing cycle running end-to-end with real Claude Batch API calls.

**Tasks:**
- [x] `workers/batch_submitter.py` — collect IDLE jobs, build batch requests, submit to Anthropic
- [x] `workers/batch_poller.py` — poll batch status, parse results, trigger price applicator
- [x] `workers/scheduler.py` — APScheduler setup, 15-min submission cycle, 5-min poll cycle, 1-hr recovery
- [x] Price applicator logic within `batch_poller.py` — fail-safe guardrail, state transitions, `price_history` writes
- [x] usage tracking table — built into `001_initial_schema.sql` directly rather than a separate `002_add_usage_events.sql` migration
- [x] Cost monitoring: token usage / cache hit / cost logged to `usage_events` in both `batch_submitter.py` and `batch_poller.py`
- [x] `workers/stale_job_recovery.py` — stale job recovery, runs hourly via scheduler
- [x] Verify: `price_history` has 8 rows from seeded test cycle; state transitions confirmed via direct DB query 2026-07-01

**Success criteria:** ✅ MET — End-to-end cycle completes. Jobs transition IDLE → BATCH_SUBMITTED → SYNCED. Fail-safe guardrail covered in `tests/unit/test_repricing_engine.py`.

---

### WEEK 4 — Dashboard MVP ✅ COMPLETE (plus extras)

**Goal:** A non-technical seller can log in and understand exactly what PriceBot is doing.

**Tasks:**
- [x] Next.js 14 project scaffold in `frontend/`
- [x] `/login` and `/register` pages with Supabase Auth integration — plus password visibility toggle (eye icon), dark mode support
- [x] `/dashboard` — overview: product count, reprices today, estimated savings
- [x] `/dashboard/products` — product table: title, current price, suggested price, state badge, last updated
- [x] `PriceSuggestionCard` component — current price → suggested price + reasoning sentence + confidence badge
- [x] Platform connection wizard (Amazon only for now) — 4-step stepper
- [x] `/dashboard/billing` — current plan, product usage, upgrade CTA
- [x] Empty states: clear CTAs on every page with no data ("Connect your first store →")
- [x] **Bonus (not originally scoped for Week 4):** Full dark-mode support across every page/component, theme toggle in `DashboardNav`
- [ ] Verify: Walk a non-technical person through the UI — they understand every element without explanation *(needs a real human; no browser access available to Claude Code — hand this to founder)*

**Success criteria:** ✅ MET (pending the human-walkthrough verify above) — A seller can register, connect their Amazon store, see their products, and understand a price suggestion. `npm run build` passes with 0 TypeScript errors as of 2026-07-01.

**Known gap surfaced during QA (2026-07-01):** `/dashboard/products` had a bug where it silently showed the empty state despite products existing in the DB — root-caused to (1) a `UserResponse` unwrapping bug in `get_current_user()` (fixed) and (2) a more severe shared-singleton DB-client contamination bug in `db/client.py` that is **not yet fixed** — see Week 6 hardening notes.

---

### WEEK 5 — Etsy Connector + Beta Prep  🟡 PARTIAL — dashboard items done, Etsy connector + email + beta prep not started

**Goal:** Second platform live. Three beta users lined up with access ready to go.

**Tasks:**
- [ ] ❌ `platforms/etsy.py` — file exists but is an **empty stub (0 lines)**. Not implemented.
- [ ] ❌ Add Etsy to platform connection wizard — blocked on above
- [ ] ❌ Etsy-specific product sync and competitor data mapping — blocked on above
- [x] `/dashboard/history` — full price change log with AI reasoning, built this session along with `GET /repricing/history` (paginated, JWT-gated, tested)
- [x] `/dashboard/settings` — margin floor defaults page exists
- [ ] ❌ Beta user onboarding checklist: Loom walkthrough video script — not started (founder action)
- [ ] ❌ Email notification on price change — no Resend/Postmark/SMTP integration found anywhere in the codebase
- [ ] ❌ Identify 3 beta users from Facebook validation respondents — not started (founder action)
- [ ] Verify: Etsy full cycle works end-to-end; email notification arrives after price change — blocked, not built

**Note:** `platforms/ebay.py`, `platforms/shopify.py`, `platforms/woocommerce.py` are also empty stub files (0 lines each) — none of the Week 5+ connectors beyond Amazon exist yet. `platforms/mock.py` (156 lines) is the dev/test mock connector used for seeding test data.

**Success criteria:** ❌ NOT MET — Only one platform (Amazon) operational. Zero beta users identified.

---

### WEEK 6 — Beta Access + Hardening  ⛔ NOT STARTED

**Goal:** Beta users have access. Real usage exposes real bugs.

**Pre-req resolved (2026-07-01):** The Critical singleton contamination bug has been fixed. `db/client.py` now exports `get_auth_client()` — a fresh, uncached `create_client(url, anon_key)` per call — and `api/routers/auth.py` uses it via `get_auth_db()` for all three session-establishing operations. `get_db()`'s service_role singleton is never touched by auth flows. Live test confirmed: User B register + login + refresh → User A's product count unchanged (4/4). Regression test: `tests/integration/test_singleton_isolation.py`. 165/165 passing.

**Tasks:**
- [ ] Provision beta user accounts (free access, 2-week window)
- [ ] Send beta access + Loom walkthrough to 3 users (founder action)
- [ ] Monitor error logs daily — fix any CRITICAL or ERROR logs same day
- [ ] Monitor batch job failure rate — target <5%
- [ ] Fix top 3 issues surfaced by beta users
- [ ] Supabase performance: review slow query log, add any missing indexes
- [ ] Security audit: run pre-deployment checklist from `docs/SECURITY.md`
- [x] `/dashboard/products/[id]` — product detail page exists (161 lines) — **missing the price history chart** called out in this task; currently a detail table only
- [ ] Stripe billing portal working end-to-end (upgrade/downgrade self-serve) — blocked on Week 2 Stripe founder setup

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