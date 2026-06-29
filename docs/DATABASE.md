# DATABASE.md — Schema, RLS, Migrations

> Supabase (PostgreSQL) is the shared data layer across all three subsystems. Every table, column, index, and RLS policy is defined here. This is the source of truth — the DB must always match what is documented here.

---

## 1. Schema Overview

```
auth.users (Supabase managed)
    │
    ├── subscriptions          ← Stripe plan, tier, status
    ├── platform_connections   ← Encrypted platform API credentials
    │
    └── products               ← Product catalog across all platforms
            │
            ├── repricing_jobs     ← State machine per product per cycle
            ├── price_history      ← Full audit log of every price change
            └── batch_results      ← Raw Claude outputs for debugging
```

---

## 2. Full Table Definitions

### 2.1 subscriptions

```sql
CREATE TABLE subscriptions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL UNIQUE REFERENCES auth.users(id) ON DELETE CASCADE,
    stripe_customer_id  TEXT NOT NULL UNIQUE,
    stripe_sub_id       TEXT NOT NULL UNIQUE,
    tier                TEXT NOT NULL DEFAULT 'starter'
                            CHECK (tier IN ('starter', 'growth', 'pro')),
    status              TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'past_due', 'canceled', 'trialing')),
    current_period_end  TIMESTAMPTZ NOT NULL,
    product_count       INTEGER DEFAULT 0,            -- Updated on product sync
    overage_units       INTEGER DEFAULT 0,            -- Pro tier: units above 10,000 products
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_subscriptions_user_id ON subscriptions (user_id);
CREATE INDEX idx_subscriptions_stripe_sub_id ON subscriptions (stripe_sub_id);
```

**Written exclusively by:** Stripe webhook handler. Never updated by client-facing API.

### 2.2 platform_connections

```sql
CREATE TABLE platform_connections (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    platform            TEXT NOT NULL CHECK (platform IN (
                            'amazon', 'etsy', 'shopify', 'ebay', 'woocommerce'
                        )),
    encrypted_creds     TEXT NOT NULL,                -- AES-256-GCM blob — never expose in API
    shop_identifier     TEXT,                         -- Display name: "My Amazon Store (US)"
    is_active           BOOLEAN DEFAULT TRUE,
    last_validated      TIMESTAMPTZ,                  -- Last successful credential check
    invalidated_at      TIMESTAMPTZ,                  -- Null unless credentials failed
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (user_id, platform)
);

CREATE INDEX idx_platform_connections_user_id ON platform_connections (user_id);
CREATE INDEX idx_platform_connections_active ON platform_connections (user_id, is_active)
    WHERE is_active = TRUE;
```

### 2.3 products

```sql
CREATE TABLE products (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    platform            TEXT NOT NULL CHECK (platform IN (
                            'amazon', 'etsy', 'shopify', 'ebay', 'woocommerce'
                        )),
    platform_product_id TEXT NOT NULL,                -- ASIN, Etsy listing ID, Shopify product ID, etc.
    platform_sku        TEXT,                         -- Seller's own SKU where applicable
    title               TEXT NOT NULL,
    current_price       NUMERIC(10, 2) NOT NULL,
    cost                NUMERIC(10, 2),               -- Optional: seller's COGS
    min_margin_floor    NUMERIC(10, 2) NOT NULL DEFAULT 0, -- Absolute floor price (not percentage)
    is_tracking         BOOLEAN DEFAULT TRUE,         -- User can pause tracking per product
    state               TEXT NOT NULL DEFAULT 'IDLE'
                            CHECK (state IN ('IDLE', 'BATCH_SUBMITTED', 'PROCESSING', 'SYNCED', 'FAILED')),
    last_repriced_at    TIMESTAMPTZ,
    last_synced_at      TIMESTAMPTZ,                  -- Last product catalog sync from platform
    reprice_cycle_count INTEGER DEFAULT 0,            -- Rolling daily counter (reset at midnight)
    fail_reason         TEXT,                         -- Last failure reason, cleared on success
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (user_id, platform, platform_product_id)
);

CREATE INDEX idx_products_user_id ON products (user_id);
CREATE INDEX idx_products_state ON products (state);
CREATE INDEX idx_products_tracking ON products (user_id, is_tracking, state)
    WHERE is_tracking = TRUE AND state = 'IDLE';     -- Scheduler query optimization
CREATE INDEX idx_products_platform ON products (user_id, platform);
```

> **Note on `min_margin_floor`:** Stored as an absolute price (e.g., $15.60), not a percentage. The UI allows sellers to input a percentage, but it is converted to absolute at save time: `floor = cost × (1 + margin_pct / 100)`. This simplifies guardrail enforcement in the applicator.

### 2.4 repricing_jobs

```sql
CREATE TABLE repricing_jobs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    product_id          UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    platform            TEXT NOT NULL,
    state               TEXT NOT NULL DEFAULT 'IDLE'
                            CHECK (state IN ('IDLE', 'BATCH_SUBMITTED', 'PROCESSING', 'SYNCED', 'FAILED')),
    batch_id            TEXT,                         -- Anthropic batch ID when BATCH_SUBMITTED
    anthropic_custom_id TEXT,                         -- "{user_id}:{product_id}" for batch result lookup
    fail_reason         TEXT,
    retry_count         INTEGER DEFAULT 0,
    scheduled_at        TIMESTAMPTZ DEFAULT NOW(),
    submitted_at        TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_repricing_jobs_user_id ON repricing_jobs (user_id);
CREATE INDEX idx_repricing_jobs_batch_id ON repricing_jobs (batch_id)
    WHERE batch_id IS NOT NULL;
CREATE INDEX idx_repricing_jobs_state ON repricing_jobs (state, updated_at)
    WHERE state IN ('BATCH_SUBMITTED', 'FAILED');    -- Poller + recovery queries
```

### 2.5 price_history

```sql
CREATE TABLE price_history (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    product_id          UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    repricing_job_id    UUID REFERENCES repricing_jobs(id),
    platform            TEXT NOT NULL,
    old_price           NUMERIC(10, 2) NOT NULL,
    new_price           NUMERIC(10, 2) NOT NULL,
    price_delta         NUMERIC(10, 2) GENERATED ALWAYS AS (new_price - old_price) STORED,
    strategy            TEXT CHECK (strategy IN ('undercut', 'match', 'premium', 'hold')),
    confidence          INTEGER CHECK (confidence BETWEEN 0 AND 100),
    reasoning           TEXT,                         -- AI's plain-English explanation
    was_auto_applied    BOOLEAN DEFAULT FALSE,        -- False = Starter manual apply
    competitor_low      NUMERIC(10, 2),               -- Lowest competitor at time of decision
    competitor_count    INTEGER,                      -- Number of competitors analyzed
    applied_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_price_history_product ON price_history (product_id, applied_at DESC);
CREATE INDEX idx_price_history_user ON price_history (user_id, applied_at DESC);
```

### 2.6 batch_results

```sql
CREATE TABLE batch_results (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    batch_id            TEXT NOT NULL,
    product_id          UUID NOT NULL REFERENCES products(id),
    raw_response        JSONB,                        -- Full Claude response for debugging
    parse_error         TEXT,                         -- Set if JSON parse failed
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_batch_results_batch_id ON batch_results (batch_id);
```

> Batch results are retained for 30 days, then purged by a scheduled pg_cron job.

### 2.7 usage_events

```sql
CREATE TABLE usage_events (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    event_type          TEXT NOT NULL CHECK (event_type IN (
                            'batch_submitted', 'batch_completed', 'price_applied',
                            'credential_validated', 'sync_completed'
                        )),
    platform            TEXT,
    product_count       INTEGER,
    tokens_input        INTEGER,
    tokens_output       INTEGER,
    tokens_cache_read   INTEGER,
    estimated_cost_usd  NUMERIC(10, 6),
    metadata            JSONB,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_usage_events_user_date ON usage_events (user_id, created_at DESC);
```

---

## 3. Row-Level Security (RLS)

All RLS policies live in `db/rls_policies.sql`. Every table must have RLS enabled.

```sql
-- ============================================================
-- ENABLE RLS ON ALL TABLES
-- ============================================================
ALTER TABLE subscriptions        ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE products             ENABLE ROW LEVEL SECURITY;
ALTER TABLE repricing_jobs       ENABLE ROW LEVEL SECURITY;
ALTER TABLE price_history        ENABLE ROW LEVEL SECURITY;
ALTER TABLE batch_results        ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_events         ENABLE ROW LEVEL SECURITY;

-- ============================================================
-- subscriptions
-- ============================================================
CREATE POLICY "Users read own subscription"
ON subscriptions FOR SELECT
USING (user_id = auth.uid());

-- Only service role (webhook handler) may write subscriptions
CREATE POLICY "Service role manages subscriptions"
ON subscriptions FOR ALL
USING (auth.role() = 'service_role');

-- ============================================================
-- platform_connections
-- ============================================================
CREATE POLICY "Users manage own platform connections"
ON platform_connections FOR ALL
USING (user_id = auth.uid());

-- ============================================================
-- products
-- ============================================================
CREATE POLICY "Users manage own products"
ON products FOR ALL
USING (user_id = auth.uid());

-- ============================================================
-- repricing_jobs
-- ============================================================
CREATE POLICY "Users read own repricing jobs"
ON repricing_jobs FOR SELECT
USING (user_id = auth.uid());

-- Only service role (workers) may write repricing jobs
CREATE POLICY "Service role manages repricing jobs"
ON repricing_jobs FOR INSERT, UPDATE, DELETE
USING (auth.role() = 'service_role');

-- ============================================================
-- price_history
-- ============================================================
CREATE POLICY "Users read own price history"
ON price_history FOR SELECT
USING (user_id = auth.uid());

CREATE POLICY "Service role manages price history"
ON price_history FOR INSERT, UPDATE, DELETE
USING (auth.role() = 'service_role');

-- ============================================================
-- batch_results (internal only — users never read this directly)
-- ============================================================
CREATE POLICY "Service role only"
ON batch_results FOR ALL
USING (auth.role() = 'service_role');

-- ============================================================
-- usage_events
-- ============================================================
CREATE POLICY "Users read own usage"
ON usage_events FOR SELECT
USING (user_id = auth.uid());

CREATE POLICY "Service role manages usage"
ON usage_events FOR INSERT, UPDATE, DELETE
USING (auth.role() = 'service_role');
```

---

## 4. Migration Strategy

### 4.1 File Naming
All migrations are in `db/migrations/` with sequential numeric prefix:
```
db/migrations/
  001_initial_schema.sql
  002_add_usage_events.sql
  003_add_product_sync_columns.sql
  ...
```

### 4.2 Migration Rules
- Migrations are **append-only** — never edit a migration that has been applied
- Each migration is idempotent where possible (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`)
- Every migration that adds a table must also include the RLS policy for that table in the same file
- Breaking schema changes (column removal, type changes) require a two-step migration: deprecate in step 1, remove in step 2 after all code references are cleared

### 4.3 Applying Migrations
```bash
# Apply a migration
psql "$SUPABASE_DB_URL" -f db/migrations/001_initial_schema.sql

# Or via Supabase CLI
supabase db push
```

---

## 5. Key Indexes Rationale

| Index | Table | Purpose |
|---|---|---|
| `(user_id, is_tracking, state)` partial | `products` | Scheduler pickup query — only IDLE + tracked products |
| `(state, updated_at)` partial | `repricing_jobs` | Poller query for BATCH_SUBMITTED jobs + recovery for FAILED |
| `(batch_id)` partial | `repricing_jobs` | Poller batch lookup — null batch_ids excluded |
| `(product_id, applied_at DESC)` | `price_history` | Product detail page — fetch recent history |
| `(user_id, applied_at DESC)` | `price_history` | Dashboard overview — user's recent activity |

---

## 6. Automated Maintenance (pg_cron)

```sql
-- Purge batch_results older than 30 days (runs daily at 3:00 AM UTC)
SELECT cron.schedule(
    'purge-old-batch-results',
    '0 3 * * *',
    $$DELETE FROM batch_results WHERE created_at < NOW() - INTERVAL '30 days'$$
);

-- Reset reprice_cycle_count daily at midnight UTC
SELECT cron.schedule(
    'reset-reprice-counts',
    '0 0 * * *',
    $$UPDATE products SET reprice_cycle_count = 0$$
);

-- Archive price_history older than 1 year (move to cold storage or delete)
SELECT cron.schedule(
    'archive-old-price-history',
    '0 4 * * 0',  -- Weekly, Sunday 4 AM
    $$DELETE FROM price_history WHERE applied_at < NOW() - INTERVAL '1 year'$$
);
```