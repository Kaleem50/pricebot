# PRICING.md — Subscription Tiers & Business Rules

> Pricing logic is enforced in code, not in the UI or frontend. The frontend displays tier information — it does not enforce it. All enforcement lives in `api/middleware/auth_guard.py` and `workers/scheduler.py`.

---

## 1. Subscription Tiers

| Feature | Starter $9/mo | Growth $29/mo | Pro $59/mo |
|---|---|---|---|
| **Products tracked** | 50 | 500 | 10,000* |
| **Platforms connected** | 1 | 3 | 5 (all) |
| **Reprice cycles/day** | 3 | 6 | 12 |
| **Auto-apply prices** | ❌ Suggestions only | ✅ Auto | ✅ Auto |
| **AI margin optimizer** | ❌ | ✅ | ✅ |
| **Price history** | 30 days | 90 days | 1 year |
| **API access** | ❌ | ❌ | ✅ |
| **Priority support** | ❌ | ❌ | ✅ |
| **Email alerts** | ✅ | ✅ | ✅ |

*Pro includes 10,000 products. Overages: +$10 per additional 1,000 products above 10,000.

---

## 2. Tier Enforcement — Code Locations

| Rule | Enforced In | How |
|---|---|---|
| Product count cap | `workers/scheduler.py` | Count products before building batch — reject overage |
| Platform count cap | `api/routers/platforms.py` | Count active connections before allowing new connect |
| Reprice cycle frequency | `workers/scheduler.py` | Check `reprice_cycle_count` vs tier limit before scheduling |
| Auto-apply (Growth/Pro only) | `workers/batch_poller.py` | Check tier before calling `apply_price()` |
| API access (Pro only) | `api/middleware/auth_guard.py` | `require_tier(Tier.PRO)` dependency |
| Price history depth | `api/routers/products.py` | Query `price_history` with date filter per tier |
| Tier source of truth | `db.subscriptions.tier` | Always read from DB — never trust client |

---

## 3. Tier Constants (Code Reference)

```python
# api/models/tiers.py
from enum import IntEnum

class Tier(IntEnum):
    STARTER = 1
    GROWTH = 2
    PRO = 3

TIER_LIMITS = {
    Tier.STARTER: TierLimits(
        max_products=50,
        max_platforms=1,
        max_daily_reprice_cycles=3,
        auto_apply=False,
        price_history_days=30,
        api_access=False,
    ),
    Tier.GROWTH: TierLimits(
        max_products=500,
        max_platforms=3,
        max_daily_reprice_cycles=6,
        auto_apply=True,
        price_history_days=90,
        api_access=False,
    ),
    Tier.PRO: TierLimits(
        max_products=10_000,
        max_platforms=5,
        max_daily_reprice_cycles=12,
        auto_apply=True,
        price_history_days=365,
        api_access=True,
        overage_per_1k_products_usd=10.00,
    ),
}
```

---

## 4. Stripe Integration

### 4.1 Product & Price IDs (Env Vars)
```bash
STRIPE_STARTER_PRICE_ID=price_...     # $9/month recurring
STRIPE_GROWTH_PRICE_ID=price_...      # $29/month recurring
STRIPE_PRO_PRICE_ID=price_...         # $59/month recurring
STRIPE_PRO_OVERAGE_PRICE_ID=price_... # $10 per unit (usage-based, Pro only)
```

### 4.2 Webhook Events Handled

| Stripe Event | Action |
|---|---|
| `customer.subscription.created` | Create `subscriptions` row, set `tier` |
| `customer.subscription.updated` | Update `tier` and `status` |
| `customer.subscription.deleted` | Set `status = 'canceled'` — pause all repricing |
| `invoice.payment_failed` | Set `status = 'past_due'` — pause repricing, email user |
| `invoice.payment_succeeded` | Set `status = 'active'` if was `past_due` |

### 4.3 Webhook Handler Contract

```python
# api/routers/billing.py
@router.post("/billing/webhook")
async def stripe_webhook(request: Request):
    """
    All Stripe events arrive here.
    HMAC signature verified before any processing.
    Idempotent — safe to receive the same event multiple times.
    """
    payload = await request.body()
    sig = request.headers.get("Stripe-Signature")
    event = stripe.Webhook.construct_event(
        payload, sig, settings.STRIPE_WEBHOOK_SECRET  # raises on invalid sig
    )

    handlers = {
        "customer.subscription.created": handle_subscription_created,
        "customer.subscription.updated": handle_subscription_updated,
        "customer.subscription.deleted": handle_subscription_deleted,
        "invoice.payment_failed": handle_payment_failed,
        "invoice.payment_succeeded": handle_payment_succeeded,
    }

    handler = handlers.get(event["type"])
    if handler:
        await handler(event["data"]["object"], db)

    return {"status": "ok"}
```

All handlers are idempotent — if the same webhook event arrives twice, the result is the same. Use `stripe_sub_id` as the idempotency key.

### 4.4 Subscription Pause on Cancellation/Past Due
When `status = 'canceled'` or `'past_due'`:
1. Scheduler skips this user's products entirely
2. Dashboard shows a prominent "Subscription paused" banner with reactivation CTA
3. Existing product data and price history are retained — not deleted
4. Repricing resumes automatically when `status` returns to `'active'`

### 4.5 Billing Portal
Growth and Pro users can self-serve plan changes via Stripe Customer Portal:

```python
@router.post("/billing/portal")
async def create_portal_session(current_user: AuthenticatedUser = Depends(get_current_user)):
    session = stripe.billing_portal.Session.create(
        customer=current_user.stripe_customer_id,
        return_url=f"{settings.FRONTEND_URL}/dashboard/billing",
    )
    return {"url": session.url}
```

---

## 5. Pro Overage Billing

### 5.1 Trigger Condition
- User is on Pro tier
- Active product count exceeds 10,000

### 5.2 Calculation (runs at scheduler time)

```python
def calculate_overage_units(product_count: int, tier: Tier) -> int:
    """
    Calculate billable overage units for Pro tier.
    Returns 0 if no overage or not Pro tier.
    """
    if tier != Tier.PRO:
        return 0
    overage_products = max(0, product_count - TIER_LIMITS[Tier.PRO].max_products)
    return math.ceil(overage_products / 1000)  # Each unit = 1,000 products
```

### 5.3 Stripe Usage Event
```python
# Emit usage record for overage billing
stripe.SubscriptionItem.create_usage_record(
    subscription_item_id=settings.STRIPE_PRO_OVERAGE_PRICE_ID,
    quantity=overage_units,
    action="set",  # "set" replaces, "increment" adds — use "set" for accuracy
)
```

Overage is recalculated and re-emitted to Stripe on each scheduler cycle. Stripe deduplicates within the billing period.

### 5.4 Dashboard Display
Pro users see their overage in the billing page:
```
Products: 12,450 / 10,000 included
Overage:  2,450 products → 3 units × $10 = $30 this cycle
```

---

## 6. Trial Period

At launch: **no free trial**. The product validates value with beta users (free access for 2 weeks during beta). After public launch, pricing is Starter $9 minimum — no freemium tier.

Future consideration: 7-day trial after first paying user cohort provides feedback data on conversion rates. This is a Phase 2 decision — do not implement now.

---

## 7. Upgrade/Downgrade Behavior

### Upgrade (Starter → Growth → Pro)
- Immediate effect via Stripe's `proration_behavior: 'always_invoice'`
- Tier updates in DB via webhook within seconds
- Auto-repricing enables immediately for Growth/Pro upgrades

### Downgrade (Pro/Growth → lower)
- Effective at **end of current billing period** (Stripe handles this)
- In DB: flag `pending_downgrade_tier` alongside current tier
- At period end, webhook fires `subscription.updated` → apply the downgrade
- If new product count exceeds new tier limit: notify user, set excess products `is_tracking = FALSE`

---

## 8. Pricing Display Rules (Frontend)

- Always show prices in USD
- Show margin floor in absolute dollar terms, not percentage: "Floor: $15.60" not "30% margin"
- Show AI confidence as a badge: 🟢 85%+ (High) / 🟡 60–84% (Medium) / 🔴 <60% (Low)
- Never show internal tier enum values (STARTER/GROWTH/PRO) — use "Starter Plan", "Growth Plan", "Pro Plan"
- Overage warning appears at 80% of product limit: "Using 401 of 500 products — consider upgrading"