# CLAUDE.md — PriceBot Engineering Bible

> **This is the first file Claude Code reads in every session. Read it completely before writing a single line of code. All architectural decisions, constraints, and patterns in this project flow from what is defined here.**

---

## 0. Project Identity

**PriceBot** is a production B2B SaaS that monitors competitor prices across five ecommerce platforms and uses Claude Haiku 4.5 to recommend or auto-apply optimal prices — protecting seller margins while keeping listings competitive 24/7.

**Target users:** Non-technical ecommerce sellers (Amazon FBA, Shopify, eBay, Etsy, WooCommerce) who reprice manually today, already pay $80–300/month for inferior rule-based tools, and want intelligence without complexity.

**This is not a prototype.** Every line of code ships to real users managing real revenue. Build accordingly.

---

## 1. Operator Model

| Role | Owns |
|---|---|
| **Claude Code** | 100% of code, architecture, DB schemas, API contracts, documentation, configs |
| **Founder (Operator)** | Terminal commands, API key management, strategic decisions, user conversations |

The founder does **not** write code. Claude Code is the sole engineering function. When a decision has architectural implications, surface the tradeoffs explicitly and wait for the founder's direction before proceeding.

---

## 2. Tech Stack — Locked

Do not introduce new dependencies without explicit founder approval. Every addition has cost and maintenance implications.

| Layer | Technology | Version |
|---|---|---|
| Backend language | Python | 3.11+ |
| API framework | FastAPI | Latest stable |
| Data validation | Pydantic | v2 only |
| AI engine | Claude Haiku 4.5 | Anthropic API |
| AI delivery | Anthropic Batch API | Async — mandatory |
| Database | Supabase (PostgreSQL) | Managed |
| Auth | Supabase Auth + JWT | — |
| Payments | Stripe Subscriptions | — |
| Frontend | Next.js 14 (App Router) | React 18 |
| Background jobs | APScheduler | 3.x |
| Frontend hosting | Vercel | — |
| Backend hosting | Railway or Render | — |
| Encryption | Python `cryptography` library | AES-256-GCM |

---

## 3. Folder Structure — Canonical

Every file goes in its designated location. Do not improvise new top-level directories.

```
pricebot/
├── CLAUDE.md                        ← Root context — read every session
├── docs/
│   ├── ARCHITECTURE.md              ← System design, data flows, state machines
│   ├── SECURITY.md                  ← Security model, threat matrix, guardrails
│   ├── COSTS.md                     ← AI cost model, margin math, optimization rules
│   ├── PLATFORMS.md                 ← Per-platform API specs, quirks, build order
│   ├── DATABASE.md                  ← Full schema, RLS policies, migration strategy
│   ├── PRICING.md                   ← Subscription tiers, enforcement rules, overages
│   └── ROADMAP.md                   ← 8-week build sequence, task queue, milestones
│
├── core/
│   ├── repricing_engine.py          ← BUILT ✅ — AI brain, platform-agnostic
│   └── __init__.py
│
├── platforms/
│   ├── base.py                      ← Abstract connector (all platforms implement this)
│   ├── amazon.py                    ← Amazon SP-API
│   ├── etsy.py                      ← Etsy API
│   ├── shopify.py                   ← Shopify Admin API
│   ├── ebay.py                      ← eBay API
│   └── woocommerce.py               ← WooCommerce REST API
│
├── api/
│   ├── main.py                      ← FastAPI app entrypoint
│   ├── dependencies.py              ← Shared FastAPI deps (DB pool, current_user)
│   ├── routers/
│   │   ├── auth.py
│   │   ├── products.py
│   │   ├── repricing.py
│   │   ├── platforms.py
│   │   └── billing.py
│   └── middleware/
│       ├── auth_guard.py            ← JWT validation, user_id extraction, tier enforcement
│       └── rate_limiter.py          ← Token-bucket rate limiting per user/tier
│
├── workers/
│   ├── batch_submitter.py           ← Collects IDLE jobs → submits Anthropic Batch API
│   ├── batch_poller.py              ← Polls batch results → applies prices
│   └── scheduler.py                 ← APScheduler orchestration (runs every 15 min)
│
├── db/
│   ├── client.py                    ← Supabase pool singleton — only DB entry point
│   ├── migrations/                  ← Sequential SQL files: 001_initial.sql, 002_*.sql
│   └── rls_policies.sql             ← Row-Level Security — source of truth
│
├── frontend/
│   ├── app/                         ← Next.js App Router pages
│   ├── components/                  ← Reusable UI components
│   └── lib/                         ← API client, auth helpers, TypeScript types
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
│
├── demo.py                          ← BUILT ✅ — sample data test runner
├── requirements.txt                 ← BUILT ✅
├── .env.example                     ← BUILT ✅
└── Makefile                         ← Dev commands: make dev, make test, make migrate
```

---

## 4. Coding Standards — Non-Negotiable

### 4.1 PEP 8 Compliance
- All Python code is PEP 8 compliant — use `black` for formatting, `ruff` for linting
- Line length: 88 characters (black default)
- Every public class, method, and function has a docstring
- Every module has a top-level docstring describing its role

### 4.2 Type Hints — Mandatory Everywhere
```python
# Every function signature — no exceptions
def calculate_margin(
    current_price: float,
    cost: float,
    min_margin_pct: float
) -> float:
    """Calculate the minimum safe price given cost and margin floor."""
    return cost * (1 + min_margin_pct / 100)
```

No `Any` types unless absolutely unavoidable, and always with an inline comment explaining why.

### 4.3 Pydantic v2 — All Data Contracts
Every boundary between modules uses a typed Pydantic model. Never pass raw `dict` objects between layers.

```python
from pydantic import BaseModel, Field, field_validator
from typing import Literal

class RepricingJob(BaseModel):
    job_id: str
    user_id: str
    product_id: str
    platform: Literal["amazon", "etsy", "shopify", "ebay", "woocommerce"]
    state: Literal["IDLE", "BATCH_SUBMITTED", "PROCESSING", "SYNCED", "FAILED"]
    batch_id: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("platform")
    @classmethod
    def validate_platform(cls, v: str) -> str:
        allowed = {"amazon", "etsy", "shopify", "ebay", "woocommerce"}
        if v not in allowed:
            raise ValueError(f"Platform must be one of: {allowed}")
        return v
```

### 4.4 Structured Logging — No print()
```python
import logging
import json

logger = logging.getLogger(__name__)

# Always include context in extra= — never interpolate into the message string
logger.info("Batch submitted to Anthropic", extra={
    "user_id": user_id,
    "job_id": job_id,
    "product_count": len(products),
    "platform": platform,
})

logger.critical("Fail-safe guardrail triggered — price update aborted", extra={
    "product_id": product_id,
    "claude_recommended": claude_price,
    "floor_price": floor_price,
    "delta": floor_price - claude_price,
})
```

Log levels: `DEBUG` (dev only), `INFO` (normal operations), `WARNING` (recoverable anomalies), `ERROR` (operation failed, retry possible), `CRITICAL` (security violation or safety guardrail triggered — always alerts).

**`print()` is a linting error in this codebase. Zero exceptions.**

### 4.5 Error Handling — No Silent Failures
```python
# Correct — specific exception, structured log, meaningful re-raise
try:
    result = await anthropic_client.batches.results(batch_id)
except anthropic.APIStatusError as e:
    logger.error("Anthropic batch poll failed", extra={
        "batch_id": batch_id,
        "status_code": e.status_code,
        "message": str(e),
    })
    raise BatchPollError(f"Batch {batch_id} poll failed: {e}") from e
```

Never swallow exceptions with bare `except: pass`. Every caught exception is logged at the appropriate level with context.

---

## 5. Architecture Rules — Structural Constraints

### 5.1 No Raw Database Connections
`db/client.py` is the **only** place a Supabase client is instantiated. All database access goes through the pool singleton it exposes. No inline `supabase.create_client()` in routers, workers, or connectors.

```python
# db/client.py — the only place this call exists
from supabase import create_client, Client
from functools import lru_cache

@lru_cache(maxsize=1)
def get_db() -> Client:
    """Return the shared Supabase client singleton."""
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
```

### 5.2 State Machine for All Batch Jobs — Enforced in DB
Repricing jobs have an explicit `state` column. Valid transitions only:

```
IDLE → BATCH_SUBMITTED → PROCESSING → SYNCED
             ↓                ↓
          FAILED           FAILED
                             ↓
                    (manual retry) → IDLE
```

The worker is **stateless** — reads state from DB, acts, writes new state. Worker restarts at any point during a 15-minute batch window are completely safe. No in-memory job state. See `docs/ARCHITECTURE.md` for full state machine spec.

### 5.3 Fail-Safe Repricing Guardrail — CRITICAL — NEVER BYPASS
This exact pattern must exist in every code path that writes a price to any platform:

```python
# MANDATORY GUARDRAIL — this line is not optional
final_price = max(claude_recommended_price, product.cost + product.min_margin_floor)

if final_price != claude_recommended_price:
    logger.warning("Guardrail applied — Claude price overridden", extra={
        "product_id": product.id,
        "claude_price": claude_recommended_price,
        "final_price": final_price,
        "floor": product.cost + product.min_margin_floor,
    })
```

If Claude returns invalid JSON, a null price, or raises any exception:
1. Set job state to `FAILED`
2. Log at `CRITICAL`
3. Do **not** apply any price to the platform
4. Surface the failure clearly in the dashboard

### 5.4 Tenant Isolation — user_id From JWT Only
`user_id` is always extracted from the validated JWT token in `auth_guard.py`. It is **never** read from the request body, query params, or URL path params for authorization decisions.

```python
# Correct
async def get_products(
    current_user: User = Depends(get_current_user),  # user_id from validated JWT
    db: Client = Depends(get_db),
) -> list[Product]:
    return db.table("products").select("*").eq("user_id", current_user.id).execute()

# WRONG — never do this
async def get_products(user_id: str = Query(...)):  # user_id from request — NEVER
    ...
```

---

## 6. Security Constraints — See `docs/SECURITY.md` for Full Spec

| Constraint | Rule |
|---|---|
| Platform API credentials | AES-256-GCM encrypted before DB write. Decrypted in memory at runtime only. Never in logs. |
| Row-Level Security | Every Supabase table has RLS policies. New tables without RLS are a security bug. |
| Rate limiting | Token-bucket limiter on all API routes. Hard cap on Batch API submissions per user/hour. |
| Stripe tier | Tier read from Stripe subscription record in DB. Never trust client-supplied tier value. |
| Secrets in code | Zero tolerance. No API keys, tokens, or passwords in source code or log output. |
| Cross-tenant queries | Every DB query filters by `user_id`. Missing filter is a data leak — treat as critical bug. |

---

## 7. Cost Rules — AI Spend is a Direct Margin Line

### 7.1 Model Lock — Haiku Only
**Always use `claude-haiku-4-5`. Never use Sonnet or Opus for repricing jobs.** Haiku is 10–20× cheaper and more than sufficient for structured repricing logic. If a feature genuinely requires a more capable model, surface this to the founder before implementing — do not silently upgrade.

### 7.2 Batch API — Mandatory for All Repricing
All repricing jobs use `anthropic.batches.create()`. Synchronous `anthropic.messages.create()` is only permitted for:
- Real-time user-facing features where 15-minute delay is unacceptable (e.g., a "test my setup" preview on onboarding)
- Developer tooling and tests

Batch API provides a mandatory 50% cost reduction. Using sync API for repricing is a margin violation.

### 7.3 Prompt Caching — Mandatory
The repricing system prompt is identical across all calls. It must be tagged for caching:

```python
system_prompt_block = {
    "type": "text",
    "text": REPRICING_SYSTEM_PROMPT,
    "cache_control": {"type": "ephemeral"},  # MANDATORY — ~90% input token savings
}
```

### 7.4 Tier-Based Frequency Enforcement
Reprice frequency caps are enforced in the scheduler **before** any job is dispatched:

| Tier | Max Daily Reprice Cycles |
|---|---|
| Starter | 3× per day |
| Growth | 6× per day |
| Pro | 12× per day |

Exceeding these limits wastes AI budget and degrades margins. The scheduler hard-checks this against the user's tier from DB before queuing jobs.

Full cost model with per-user margins: `docs/COSTS.md`.

---

## 8. How Claude Code Behaves in This Repo

### 8.1 Read Before Writing
Before implementing any feature, read:
1. This file (`CLAUDE.md`) — already done if you're reading this
2. `docs/ARCHITECTURE.md` — understand where the feature fits
3. The relevant `docs/` file for the domain (e.g., `docs/SECURITY.md` before touching auth)

### 8.2 One Task at a Time
Complete the current task fully — tests, types, logging, docstrings — before moving to the next. Partial implementations with `# TODO: add error handling` are not acceptable in this codebase.

### 8.3 Always Check Security
Before marking any task complete, run the mental checklist in Section 10 of this file. If any item fails, fix it before declaring done.

### 8.4 Surface Blockers Immediately
If a task requires an environment variable, API key, or external action that only the founder can perform, say so explicitly and stop. Do not write placeholder logic that silently fails.

### 8.5 Session Snapshot at Task Completion
At the end of every completed task, output the snapshot block defined in Section 11.

---

## 9. What Claude Code Must Never Do

| ❌ Prohibited Action | Why |
|---|---|
| Call `anthropic.messages.create()` for repricing jobs | Bypasses Batch API — destroys margins |
| Use `claude-sonnet-*` or `claude-opus-*` for repricing | Wrong model — 10–20× cost increase |
| Instantiate Supabase client outside `db/client.py` | Breaks connection pooling — connection exhaustion |
| Read `user_id` from request body for authorization | Tenant isolation bypass — data leak |
| Write a price to any platform without the fail-safe guardrail | Unsafe price — business-critical failure |
| Store platform credentials in plaintext | Security violation |
| Use `print()` instead of `logger.*` | Unstructured, unsearchable, silently swallowed in prod |
| Create a new Supabase table without RLS policies | Data leak for all users on that table |
| Introduce a new dependency without surfacing it to the founder | Unreviewed maintenance + cost risk |
| Refactor `core/repricing_engine.py` without explicit instruction | The AI brain is tested and stable |
| Skip type hints or Pydantic models to ship faster | Creates cascading type errors at runtime |
| Use `Any` type without an inline justification comment | Defeats the purpose of type safety |
| Swallow exceptions with bare `except: pass` | Silent failures are invisible failures |
| Hard-code tier limits, prices, or product caps in business logic | Configuration belongs in DB or env — not code |

---

## 10. Pre-Completion Security Checklist

Run before marking any task done:

- [ ] No secrets, keys, or credentials appear anywhere in code or log output
- [ ] Every new API endpoint validates JWT and reads `user_id` from token only
- [ ] Every new DB query has a `.eq("user_id", current_user.id)` filter
- [ ] New Supabase tables have RLS policies written and applied
- [ ] Platform credentials encrypted before write, decrypted in-memory only
- [ ] Fail-safe guardrail `max(claude_price, cost + floor)` present in every price-write path
- [ ] Rate limiting applied to new routes
- [ ] Zero `print()` statements — structured `logger.*` only
- [ ] Pydantic v2 models defined for all new data shapes crossing module boundaries
- [ ] Batch job state transitions follow the defined state machine
- [ ] Tier limits validated in scheduler before any job is dispatched
- [ ] All functions have type hints and docstrings
- [ ] All new code has corresponding test cases in `tests/`

---

## 11. Session Snapshot Protocol

Output this block at the end of every completed task:

```
=== PRICEBOT SESSION SNAPSHOT ===
Date       : YYYY-MM-DD
Session    : [short description of what was worked on]

Completed  :
  - [bullet: what was built]
  - [bullet: what was built]

Files      :
  - [path/to/file.py] — [created|modified]
  - [path/to/file.py] — [created|modified]

State      : [one paragraph — what the system can do end-to-end right now]

Next task  : [exact, unambiguous description of the next thing to build]

Blockers   : [anything the founder must do before building continues — or "None"]
=================================
```

---

## 12. Current Build Status

_Last verified 2026-07-01 via live QA pass + full pytest run (163/163 passing). Full detail in `docs/ROADMAP.md` §1._

| Component | Status |
|---|---|
| `core/repricing_engine.py` | ✅ Built and tested |
| `demo.py` | ✅ Built — tests Etsy, Amazon, eBay with sample data |
| Week 1 — Foundation (DB, FastAPI, auth, rate limiter) | ✅ Complete |
| Week 2 — Amazon connector + billing code | ✅ Complete — Stripe account setup still a founder action |
| Week 3 — Worker pipeline (batch submit/poll/scheduler) | ✅ Complete |
| Week 4 — Dashboard MVP + dark mode + password toggle | ✅ Complete |
| Week 5 — `/dashboard/history`, `/dashboard/settings`, history API | ✅ Complete |
| Week 5 — Etsy connector, email notifications, beta prep | ❌ Not built |
| `platforms/ebay.py`, `shopify.py`, `woocommerce.py` | ❌ Empty stub files |
| Weeks 6–8 | ⛔ Not started |
| 🔴 **Critical bug** — shared `db/client.py` singleton corrupted by any `/auth/register`, `/auth/login`, or `/auth/refresh` call, silently breaking other users' concurrent queries | **Found 2026-07-01, unfixed — must fix before Week 6 beta access** |

**Current active task:** Fix the shared-singleton DB contamination bug (see `docs/ROADMAP.md` Week 6 pre-req), then resume Week 5 (Etsy connector + email notifications) or proceed to Week 6 hardening.

Refer to `docs/ROADMAP.md` for the full sequenced build queue.