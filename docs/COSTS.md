# COSTS.md — AI Cost Model & Margin Architecture

> AI cost is a direct margin line. Every architectural decision in the worker pipeline has a cost implication. This document is the source of truth for all cost calculations, optimization rules, and margin enforcement.

---

## 1. Model Selection — Haiku Only

**Claude Haiku 4.5 is the only model permitted for repricing jobs.**

| Model | Input (per M tokens) | Output (per M tokens) | Use in PriceBot |
|---|---|---|---|
| Claude Haiku 4.5 | $1.00 | $5.00 | ✅ All repricing jobs |
| Claude Sonnet 4.x | ~$3.00 | ~$15.00 | ❌ Prohibited for repricing |
| Claude Opus 4.x | ~$15.00 | ~$75.00 | ❌ Prohibited for repricing |

Haiku is sufficient for structured JSON repricing decisions. Using a more capable model is not a quality improvement — it is a margin destruction event.

If a future feature genuinely requires a more capable model (e.g., complex multi-platform strategy reasoning), surface this to the founder with a cost projection before implementing.

---

## 2. Cost Optimizations — Both Mandatory Before First User

### 2.1 Anthropic Batch API — 50% Cost Reduction

All repricing jobs are submitted via `POST /v1/message-batches`, never via synchronous `POST /v1/messages`.

- Batch API provides a **50% discount** on all Claude API costs
- Maximum latency: 15 minutes (acceptable — repricing is not time-critical to the second)
- Batch size: up to 10,000 requests per batch — all products for one user go in one batch
- Implementation: `workers/batch_submitter.py`

```python
# Correct — always use batch API for repricing
batch = anthropic.batches.create(
    requests=[
        MessageCreateParamsNonStreaming(
            custom_id=f"{user_id}:{product.id}",
            params={
                "model": "claude-haiku-4-5",
                "max_tokens": 512,           # Repricing output is small — cap tightly
                "system": [system_prompt_with_cache_control],
                "messages": [{"role": "user", "content": product_context}],
            }
        )
        for product in products
    ]
)

# Wrong — never do this for repricing
response = anthropic.messages.create(
    model="claude-haiku-4-5",
    messages=[...],
)
```

### 2.2 Prompt Caching — ~90% Input Token Savings on System Prompt

The repricing system prompt is identical for every call. It contains the rules, output schema, and reasoning framework — typically 500–1,000 tokens. Caching it eliminates ~90% of repeated input token costs.

```python
REPRICING_SYSTEM_PROMPT = """
You are a pricing intelligence engine for an ecommerce repricing tool.
[... full prompt content ...]
Always respond with valid JSON matching this exact schema: {...}
"""

# The system prompt block — cache_control is MANDATORY
system_prompt_block = {
    "type": "text",
    "text": REPRICING_SYSTEM_PROMPT,
    "cache_control": {"type": "ephemeral"},  # DO NOT REMOVE
}
```

Prompt caching applies automatically after the first call in a 5-minute window. The cache hit rate approaches 100% during a scheduler cycle (all products share the same system prompt).

### 2.3 Combined Effect

| Optimization | Applied | Effective Cost vs Baseline |
|---|---|---|
| No optimization (baseline) | Neither | 100% |
| Batch API only | Yes | 50% |
| Prompt caching only | Yes | ~15% (on input tokens) |
| **Batch API + Prompt Caching** | **Both** | **~5–10% of baseline** |

**This is a 90–95% cost reduction. Both must be active before the first real user.**

---

## 3. Per-Tier Cost Model

### 3.1 Token Accounting per Product per Reprice Call

| Component | Tokens | Notes |
|---|---|---|
| System prompt (input) | ~800 | Cached after first call — billed at $0.10/M (10% rate) |
| Product context (input) | ~200 | Not cached — billed at $1.00/M |
| Competitor data (input) | ~300 | Not cached — billed at $1.00/M |
| AI recommendation (output) | ~150 | Always billed at $5.00/M |

Effective cost per product per call (with both optimizations):
- Input: (800 × $0.10/M) + (500 × $1.00/M) = $0.000080 + $0.000500 = **$0.000580**
- Output: 150 × $5.00/M = **$0.000750**
- **Total per product per call: ~$0.00133**

### 3.2 Monthly Cost per User

| Tier | Products | Calls/Day | Calls/Month | Claude Cost | Batch Discount (50%) | Final AI Cost |
|---|---|---|---|---|---|---|
| Starter | 50 | 3 | ~4,500 | $5.99 | −$2.99 | **$3.00** |
| Growth | 500 | 6 | ~90,000 | $119.70 | −$59.85 | **$59.85** |
| Pro | 10,000 | 12 | ~3,600,000 | — | — | See note |

> **Pro tier note:** At 10,000 products × 12 calls/day, raw AI cost approaches $143/month per user. With Batch API + prompt caching this drops to ~$14.30. This is why both optimizations are non-negotiable.

### 3.3 Full Margin Model per User (Monthly)

|  | Starter $9 | Growth $29 | Pro $59 |
|---|---|---|---|
| Revenue | $9.00 | $29.00 | $59.00 |
| Claude API cost | $3.00 | $5.18 | $20.70 |
| Stripe fee (2.9% + $0.30) | $0.56 | $1.14 | $2.01 |
| Hosting (amortized) | $0.00 | $0.50 | $2.00 |
| **Net profit/user** | **$5.44** | **$22.18** | **$34.29** |
| **Margin %** | **60%** | **76%** | **58%** |

> **Starter margin note:** At low product counts the prompt caching advantage is smaller (fewer cache hits per cycle). Margin improves significantly once the user base grows and cache hit rates stabilize.

---

## 4. Revenue at Scale

| User Count | Mix Assumption | Monthly Profit |
|---|---|---|
| 10 users | 60% Starter, 30% Growth, 10% Pro | ~$119 |
| 30 users | 50% Starter, 40% Growth, 10% Pro | ~$418 |
| 100 users | 40% Starter, 45% Growth, 15% Pro | ~$1,530 |
| 500 users | 30% Starter, 50% Growth, 20% Pro | ~$8,140 |

First business milestone: **$1,000/month profit ≈ 65–70 users at mixed tiers.**

---

## 5. Cost Guardrails in Code

### 5.1 Max Tokens Cap — Tight Output Budget
Repricing output is structured JSON — never prose. Cap `max_tokens` tightly:

```python
# 512 tokens is sufficient for full RepricingRecommendation JSON
# 1,024 is the hard ceiling — never set higher for repricing
"max_tokens": 512,
```

Every unused output token is money saved. Prompt engineering must keep responses concise.

### 5.2 Tier-Gated Frequency Cap
The scheduler enforces reprice frequency per tier before dispatching any batch:

```python
TIER_REPRICE_LIMITS = {
    Tier.STARTER: {"max_daily_cycles": 3, "max_products": 50},
    Tier.GROWTH:  {"max_daily_cycles": 6, "max_products": 500},
    Tier.PRO:     {"max_daily_cycles": 12, "max_products": 10_000},
}
```

Exceeding these limits is both a cost problem and a terms-of-service violation. The scheduler checks against today's `reprice_cycle_count` from the DB before every batch submission.

### 5.3 Pro Overage Billing
Pro users above 10,000 products are billed overages:
- +$10 per additional 1,000 products
- Calculated at scheduler time: if `product_count > 10_000`, compute overage units, flag in `subscriptions` table, Stripe usage-based billing event emitted
- This logic is in `workers/scheduler.py` — not in the frontend, not in the API layer

### 5.4 Daily Cost Budget Alert
If a single user's estimated daily AI cost exceeds $5.00 (Pro tier concern), log at `WARNING` and flag for operator review. If it exceeds $15.00, pause that user's repricing and alert.

```python
COST_WARNING_THRESHOLD_DAILY = 5.00   # USD — log WARNING
COST_PAUSE_THRESHOLD_DAILY = 15.00    # USD — pause + alert
```

---

## 6. Cost Monitoring Checklist

These metrics must be tracked in Supabase and visible to the operator:

| Metric | Tracked In | Alert Threshold |
|---|---|---|
| Tokens consumed per user per day | `usage_events` table | >100K tokens/day for Starter |
| Batch submission count per user per hour | `batch_log` table | >20/hour any tier |
| Claude API cost per user per month | Computed from usage_events | >110% of expected for tier |
| Cache hit rate (from batch results) | Logged from batch metadata | <80% hit rate = prompt caching broken |
| Failed batch rate | `repricing_jobs` FAILED count | >5% failure rate in any hour |

---

## 7. Cost Anti-Patterns — Never Do These

| Anti-Pattern | Cost Impact |
|---|---|
| `anthropic.messages.create()` for repricing (sync API) | 2× cost vs Batch API |
| Missing `cache_control` on system prompt | ~8× input token cost |
| `max_tokens=4096` on repricing calls | Wastes output budget — repricing output is <200 tokens |
| Using Sonnet or Opus for repricing | 3–75× cost vs Haiku |
| Submitting products over tier limit | Wastes AI spend on unauthorized usage |
| Not checking daily frequency cap | Can 4× a Starter user's AI cost |
| Re-submitting BATCH_SUBMITTED jobs (no state check) | Duplicate spend on same products |