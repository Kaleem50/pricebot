# ARCHITECTURE.md — PriceBot System Design

---

## 1. System Overview

PriceBot is composed of three independently deployable subsystems that communicate exclusively through Supabase. No subsystem makes HTTP calls to another subsystem at runtime.

```
┌──────────────────────────────────────────────────────────────────────┐
│  SUBSYSTEM 1 — API Layer (FastAPI on Railway/Render)                 │
│                                                                      │
│  User auth · Platform credential management · Product catalog sync   │
│  Price suggestion approval (Starter tier) · Billing + tier checks   │
│  Rate limiting · Tenant isolation                                    │
└───────────────────────────┬──────────────────────────────────────────┘
                            │ Reads/writes via Supabase client
                            ▼
┌──────────────────────────────────────────────────────────────────────┐
│  SUPABASE (Shared Data Layer)                                        │
│                                                                      │
│  users · platform_connections · products · repricing_jobs           │
│  price_history · batch_results · subscriptions                      │
└─────┬──────────────────────────────────────────────┬────────────────┘
      │ Reads/writes                                  │ Reads/writes
      ▼                                               ▼
┌──────────────────────────┐            ┌─────────────────────────────┐
│  SUBSYSTEM 2             │            │  SUBSYSTEM 3                │
│  Background Worker       │            │  Frontend Dashboard          │
│  (APScheduler on Railway)│            │  (Next.js on Vercel)        │
│                          │            │                             │
│  Scheduler (15min cycle) │            │  Product list + states      │
│  Batch Submitter         │            │  AI reasoning display       │
│  Anthropic Batch API     │            │  Platform connection wizard │
│  Batch Poller            │            │  Price history + analytics  │
│  Price Applicator        │            │  Billing portal             │
└──────────────────────────┘            └─────────────────────────────┘
```

---

## 2. Core Repricing Data Flow

End-to-end lifecycle of a single product repricing cycle:

```
STEP 1 — SCHEDULER (every 15 minutes)
  ├── Query: SELECT users WHERE next_reprice_due <= NOW()
  ├── For each user: verify subscription active, tier limits not exceeded
  └── Hand user_ids to Batch Submitter

STEP 2 — BATCH SUBMITTER
  ├── For each user:
  │   ├── Pull products WHERE state = 'IDLE' AND platform IN (user's connected platforms)
  │   ├── Enforce product count cap (tier limit)
  │   ├── For each product:
  │   │   ├── Decrypt platform credentials from DB
  │   │   ├── Call platform connector: get_competitor_prices(product)
  │   │   └── Package: {my_price, cost, min_margin, competitors, platform, context}
  │   ├── Build Anthropic Batch API request (all products → one batch per user)
  │   ├── Tag system prompt with cache_control: {"type": "ephemeral"}
  │   ├── Submit: POST /v1/message-batches
  │   └── DB update: products.state = 'BATCH_SUBMITTED', batch_id = <returned_id>
  └── Log: batch submitted, product count, estimated cost

STEP 3 — BATCH POLLER (every 5 minutes)
  ├── Query: SELECT DISTINCT batch_id FROM repricing_jobs WHERE state = 'BATCH_SUBMITTED'
  ├── For each batch_id:
  │   ├── GET /v1/message-batches/{batch_id}
  │   ├── If processing_status = 'in_progress': skip (check again in 5 min)
  │   └── If processing_status = 'ended':
  │       ├── Iterate batch results
  │       └── For each result → hand to Price Applicator

STEP 4 — PRICE APPLICATOR
  ├── Parse Claude JSON response → RepricingRecommendation model
  ├── If parse fails: state = FAILED, log CRITICAL, skip
  ├── Apply MANDATORY guardrail:
  │   final_price = max(claude_price, product.cost + product.min_margin_floor)
  ├── Check user tier:
  │   ├── Starter: write suggestion to DB, do NOT call platform API
  │   └── Growth/Pro: call platform connector: apply_price(product, final_price)
  ├── Write price_history record: {old_price, new_price, delta, strategy, reasoning, timestamp}
  └── Update product: state = 'SYNCED', last_repriced_at = NOW()
```

---

## 3. Repricing Job State Machine

### 3.1 State Definitions

| State | Meaning | Owner |
|---|---|---|
| `IDLE` | Ready to be picked up in next scheduler cycle | Scheduler resets here |
| `BATCH_SUBMITTED` | Submitted to Anthropic, awaiting batch completion | Batch Submitter writes |
| `PROCESSING` | Poller retrieved result, applicator is writing price | Batch Poller writes |
| `SYNCED` | Price successfully applied (or suggestion stored for Starter) | Price Applicator writes |
| `FAILED` | Error at any stage — requires operator review or auto-retry | Applicator/Poller writes |

### 3.2 Valid Transitions

```
IDLE ──────────────────────────→ BATCH_SUBMITTED
                                        │
                              ┌─────────┴──────────┐
                              ▼                    ▼
                          PROCESSING            FAILED ←──── (parse error,
                              │                               platform error,
                    ┌─────────┴──────────┐       │            API timeout)
                    ▼                    ▼        │
                 SYNCED              FAILED       │
                    │                             │
                    └──────────────────────────── ┘
                         (after 15 min, auto-reset to IDLE)
```

### 3.3 Enforcement Rules
- Only the scheduler may write `IDLE → BATCH_SUBMITTED`
- Only the poller may write `BATCH_SUBMITTED → PROCESSING`
- Only the price applicator may write `PROCESSING → SYNCED` or `PROCESSING → FAILED`
- The API layer (retry endpoint) may write `FAILED → IDLE`
- Any other transition is a bug — log at `ERROR` and halt

### 3.4 Stale Job Recovery
A `FAILED` job older than 1 hour is auto-reset to `IDLE` by the scheduler.
A `BATCH_SUBMITTED` job older than 2 hours with no result is considered lost — set to `FAILED` with reason `"batch_timeout"`.

---

## 4. Platform Connector Architecture

### 4.1 Abstract Base Contract

All platform connectors implement `platforms/base.py`:

```python
from abc import ABC, abstractmethod
from typing import List
from pydantic import BaseModel

class BasePlatformConnector(ABC):
    """Abstract base for all ecommerce platform connectors.

    Each connector is instantiated per-request with decrypted credentials.
    Connectors must never cache credentials beyond the request lifecycle.
    """

    def __init__(self, credentials: dict[str, str], user_id: str) -> None:
        self.user_id = user_id
        self._credentials = credentials  # decrypted, in-memory only

    @abstractmethod
    async def validate_credentials(self) -> bool:
        """Verify stored credentials are valid. Called on connect and daily."""
        ...

    @abstractmethod
    async def get_products(self) -> List[MyProduct]:
        """Pull full product catalog from this platform for this user."""
        ...

    @abstractmethod
    async def get_competitor_prices(self, product: MyProduct) -> List[CompetitorProduct]:
        """Fetch current competitor listings for a given product."""
        ...

    @abstractmethod
    async def apply_price(self, product: MyProduct, new_price: float) -> bool:
        """Push new price to platform. Returns True on success, raises on failure."""
        ...
```

### 4.2 Retry Policy (All Connectors)
```python
# Applied to get_competitor_prices() and apply_price()
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(PlatformRateLimitError),
    before_sleep=log_retry_attempt,
)
```

### 4.3 Connector Error Taxonomy
| Exception | Meaning | Worker Action |
|---|---|---|
| `PlatformAuthError` | Credentials invalid or expired | Set job FAILED, trigger re-auth notification to user |
| `PlatformRateLimitError` | Platform 429 received | Retry with backoff (3 attempts) |
| `PlatformProductNotFoundError` | Product delisted or unavailable | Set job FAILED, mark product inactive |
| `PlatformAPIError` | Unexpected platform error | Set job FAILED, log ERROR |

---

## 5. API Layer

### 5.1 Authentication Flow
```
Client → POST /auth/login (email + password)
      → Supabase Auth validates credentials
      → Returns: {access_token (JWT), refresh_token}
      → Client stores tokens, sends access_token in Authorization: Bearer header
      → auth_guard.py validates JWT on every protected request
      → Extracts user_id from token claims — this is the authoritative user_id
```

### 5.2 Endpoint Map

```
/auth
├── POST /auth/register                  Public
├── POST /auth/login                     Public
└── POST /auth/refresh                   Public (requires valid refresh_token)

/platforms                               Auth required
├── GET  /platforms                      List user's connected platforms + status
├── POST /platforms/{platform}/connect   Store encrypted credentials
├── DELETE /platforms/{platform}         Remove credentials + cancel active jobs
└── POST /platforms/{platform}/sync      Trigger manual product catalog sync

/products                                Auth required + tier check
├── GET  /products                       Paginated, filterable product list
├── GET  /products/{id}                  Product detail + last suggestion
├── PATCH /products/{id}/settings        Update min_margin_floor, tracking on/off
└── POST /products/{id}/apply            Starter only: manually apply a suggestion

/repricing                               Auth required
├── GET  /repricing/history              Paginated price change log
├── GET  /repricing/jobs                 Active + recent job states
└── POST /repricing/jobs/{id}/retry      Reset FAILED job to IDLE

/billing                                 Auth required
├── GET  /billing/subscription           Current plan, usage stats, next billing date
├── POST /billing/portal                 Create Stripe portal session URL
└── POST /billing/webhook                Stripe webhook (public, HMAC verified)
```

### 5.3 Middleware Stack (Applied in Order)
```
Request
  → Rate Limiter (token bucket, per user_id)
  → Auth Guard (JWT validation, user_id extraction)
  → Tier Enforcer (check subscription tier against requested operation)
  → Router Handler
  → Response
```

---

## 6. Frontend Architecture

### 6.1 Page Structure
```
/                       → Redirect to /dashboard or /login
/login                  → Auth page
/register               → Onboarding step 1 (email/password)
/onboarding             → Platform connection wizard (post-registration)
/dashboard              → Overview: today's reprices, savings, active jobs
/dashboard/products     → Full product table with states + suggestions
/dashboard/products/[id]→ Product detail: history chart, suggestion card, settings
/dashboard/platforms    → Connected platforms + connect new
/dashboard/history      → Full price change log with AI reasoning
/dashboard/settings     → Margin floor defaults, notification preferences
/dashboard/billing      → Plan, usage, upgrade/downgrade
```

### 6.2 PriceSuggestionCard Component Contract
Every AI suggestion rendered in the UI must display all of these — no exceptions:

```typescript
interface PriceSuggestion {
  currentPrice: number;           // "Your current price: $24.99"
  suggestedPrice: number;         // "Suggested price: $22.99"
  competitorBenchmark: number;    // "Lowest competitor: $21.50"
  marginFloor: number;            // "Your floor: $18.00 (protected)"
  strategy: "undercut" | "match" | "premium" | "hold";
  confidence: number;             // 0–100, shown as badge
  reasoning: string;              // Plain English — "3 competitors lowered prices
                                  //  in the last hour. Slight undercut keeps you
                                  //  competitive without triggering a price war."
  appliedAt?: Date;               // If Growth/Pro auto-applied
}
```

**No raw JSON, no technical field names, no database IDs are ever shown to the user.**

### 6.3 Platform Connection Wizard (Stepper)
```
Step 1: Select platform (Amazon / Etsy / Shopify / eBay / WooCommerce)
Step 2: Platform-specific credential instructions (plain English with screenshots)
Step 3: Paste credentials → "Test Connection" button → real-time validation
Step 4: Import products → progress indicator
Step 5: Set margin floor defaults → done
```
Destructive actions (disconnect platform, disable auto-repricing) always require a confirmation modal with explicit consequence text.

---

## 7. Worker Process Architecture

### 7.1 Scheduler Cycle (APScheduler, 15-minute interval)
```python
scheduler.add_job(run_batch_submission_cycle, "interval", minutes=15)
scheduler.add_job(run_batch_poll_cycle, "interval", minutes=5)
scheduler.add_job(run_stale_job_recovery, "interval", hours=1)
scheduler.add_job(run_credential_validation, "cron", hour=3)  # 3 AM daily
```

### 7.2 Stale Job Recovery Job
Runs hourly. Finds and resolves stuck states:
- `BATCH_SUBMITTED` older than 2 hours → `FAILED` with reason `batch_timeout`
- `PROCESSING` older than 30 minutes → `FAILED` with reason `applicator_timeout`
- `FAILED` older than 1 hour → `IDLE` (auto-retry once per day max)

### 7.3 Concurrent Job Safety
Each user's products are processed in a single batch per scheduler cycle. The scheduler uses a DB-level advisory lock (or Supabase `pg_advisory_lock`) to ensure no two workers process the same user simultaneously if workers are ever scaled horizontally.

---

## 8. Data Flow Diagram — Single Seller, One Product, Full Cycle

```
[Seller connects Amazon account]
  → Credentials encrypted (AES-256-GCM)
  → Stored in platform_connections table
  → Sync job runs: products imported to products table (state: IDLE)

[15 min scheduler fires]
  → Seller is due for reprice (tier: Growth, 6×/day, last run > 4 hrs ago)
  → Batch Submitter picks up 50 IDLE products
  → Calls amazon.py: get_competitor_prices() for each product
  → Builds Anthropic Batch API request (50 items, cached system prompt)
  → Submits batch → batch_id: "batch_abc123"
  → DB update: all 50 products → state: BATCH_SUBMITTED, batch_id: "batch_abc123"

[5 min poller fires, 3× over next 15 min]
  → Polls Anthropic: GET /v1/message-batches/batch_abc123
  → Response: processing_status: "in_progress" → skip
  → [5 min later] processing_status: "ended"
  → Iterates 50 results

[Price Applicator — product #7: Widget XL]
  → Claude recommendation: $18.49, strategy: "undercut", confidence: 87
  → Product cost: $12.00, min_margin_floor: 30% → floor: $15.60
  → Guardrail: max(18.49, 15.60) = $18.49 ✓ (guardrail not triggered)
  → Tier: Growth → auto-apply
  → amazon.py: apply_price(product, 18.49) → success
  → Write price_history: {old: $19.99, new: $18.49, reason: "...", timestamp}
  → Update product: state: SYNCED, last_repriced_at: now()

[Dashboard]
  → Seller sees: "Price updated: $19.99 → $18.49 — 3 competitors lowered 
     prices in the last hour. Slight undercut maintains rank without a race 
     to the bottom."
```