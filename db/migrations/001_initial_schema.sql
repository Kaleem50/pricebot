-- ============================================================
-- 001_initial_schema.sql — PriceBot Initial Database Schema
--
-- Creates all 7 tables, indexes, RLS policies, and pg_cron jobs
-- for the PriceBot foundation layer.
--
-- Apply with:
--   psql "$SUPABASE_DB_URL" -f db/migrations/001_initial_schema.sql
--
-- Idempotent: safe to re-run (uses IF NOT EXISTS throughout).
-- ============================================================


-- ============================================================
-- EXTENSIONS
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pg_cron;


-- ============================================================
-- TABLE: subscriptions
--
-- Written exclusively by the Stripe webhook handler.
-- Never updated by client-facing API routes.
-- ============================================================

CREATE TABLE IF NOT EXISTS subscriptions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL UNIQUE REFERENCES auth.users(id) ON DELETE CASCADE,
    stripe_customer_id  TEXT NOT NULL UNIQUE,
    stripe_sub_id       TEXT NOT NULL UNIQUE,
    tier                TEXT NOT NULL DEFAULT 'starter'
                            CHECK (tier IN ('starter', 'growth', 'pro')),
    status              TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'past_due', 'canceled', 'trialing')),
    current_period_end  TIMESTAMPTZ NOT NULL,
    product_count       INTEGER DEFAULT 0,
    overage_units       INTEGER DEFAULT 0,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id
    ON subscriptions (user_id);

CREATE INDEX IF NOT EXISTS idx_subscriptions_stripe_sub_id
    ON subscriptions (stripe_sub_id);


-- ============================================================
-- TABLE: platform_connections
--
-- Encrypted platform API credentials per user per platform.
-- AES-256-GCM blob — never returned in API responses.
-- ============================================================

CREATE TABLE IF NOT EXISTS platform_connections (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    platform            TEXT NOT NULL CHECK (platform IN (
                            'amazon', 'etsy', 'shopify', 'ebay', 'woocommerce'
                        )),
    encrypted_creds     TEXT NOT NULL,
    shop_identifier     TEXT,
    is_active           BOOLEAN DEFAULT TRUE,
    last_validated      TIMESTAMPTZ,
    invalidated_at      TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (user_id, platform)
);

CREATE INDEX IF NOT EXISTS idx_platform_connections_user_id
    ON platform_connections (user_id);

CREATE INDEX IF NOT EXISTS idx_platform_connections_active
    ON platform_connections (user_id, is_active)
    WHERE is_active = TRUE;


-- ============================================================
-- TABLE: products
--
-- Product catalog across all platforms for all users.
-- state column drives the repricing job state machine.
-- ============================================================

CREATE TABLE IF NOT EXISTS products (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    platform            TEXT NOT NULL CHECK (platform IN (
                            'amazon', 'etsy', 'shopify', 'ebay', 'woocommerce'
                        )),
    platform_product_id TEXT NOT NULL,
    platform_sku        TEXT,
    title               TEXT NOT NULL,
    current_price       NUMERIC(10, 2) NOT NULL,
    cost                NUMERIC(10, 2),
    min_margin_floor    NUMERIC(10, 2) NOT NULL DEFAULT 0,
    is_tracking         BOOLEAN DEFAULT TRUE,
    state               TEXT NOT NULL DEFAULT 'IDLE'
                            CHECK (state IN ('IDLE', 'BATCH_SUBMITTED', 'PROCESSING', 'SYNCED', 'FAILED')),
    last_repriced_at    TIMESTAMPTZ,
    last_synced_at      TIMESTAMPTZ,
    reprice_cycle_count INTEGER DEFAULT 0,
    fail_reason         TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (user_id, platform, platform_product_id)
);

CREATE INDEX IF NOT EXISTS idx_products_user_id
    ON products (user_id);

CREATE INDEX IF NOT EXISTS idx_products_state
    ON products (state);

-- Scheduler query optimisation: only IDLE + tracked products are picked up
CREATE INDEX IF NOT EXISTS idx_products_tracking
    ON products (user_id, is_tracking, state)
    WHERE is_tracking = TRUE AND state = 'IDLE';

CREATE INDEX IF NOT EXISTS idx_products_platform
    ON products (user_id, platform);


-- ============================================================
-- TABLE: repricing_jobs
--
-- One row per product per scheduler cycle.
-- State machine: IDLE → BATCH_SUBMITTED → PROCESSING → SYNCED | FAILED
-- ============================================================

CREATE TABLE IF NOT EXISTS repricing_jobs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    product_id          UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    platform            TEXT NOT NULL,
    state               TEXT NOT NULL DEFAULT 'IDLE'
                            CHECK (state IN ('IDLE', 'BATCH_SUBMITTED', 'PROCESSING', 'SYNCED', 'FAILED')),
    batch_id            TEXT,
    anthropic_custom_id TEXT,
    fail_reason         TEXT,
    retry_count         INTEGER DEFAULT 0,
    scheduled_at        TIMESTAMPTZ DEFAULT NOW(),
    submitted_at        TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_repricing_jobs_user_id
    ON repricing_jobs (user_id);

CREATE INDEX IF NOT EXISTS idx_repricing_jobs_batch_id
    ON repricing_jobs (batch_id)
    WHERE batch_id IS NOT NULL;

-- Poller query (BATCH_SUBMITTED) + stale recovery query (FAILED)
CREATE INDEX IF NOT EXISTS idx_repricing_jobs_state
    ON repricing_jobs (state, updated_at)
    WHERE state IN ('BATCH_SUBMITTED', 'FAILED');


-- ============================================================
-- TABLE: price_history
--
-- Immutable audit log of every price change.
-- price_delta is a generated column (new_price - old_price).
-- ============================================================

CREATE TABLE IF NOT EXISTS price_history (
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
    reasoning           TEXT,
    was_auto_applied    BOOLEAN DEFAULT FALSE,
    competitor_low      NUMERIC(10, 2),
    competitor_count    INTEGER,
    applied_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_price_history_product
    ON price_history (product_id, applied_at DESC);

CREATE INDEX IF NOT EXISTS idx_price_history_user
    ON price_history (user_id, applied_at DESC);


-- ============================================================
-- TABLE: batch_results
--
-- Raw Claude outputs stored for 30 days for debugging.
-- Users never read this directly — service role only.
-- ============================================================

CREATE TABLE IF NOT EXISTS batch_results (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    batch_id            TEXT NOT NULL,
    product_id          UUID NOT NULL REFERENCES products(id),
    raw_response        JSONB,
    parse_error         TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_batch_results_batch_id
    ON batch_results (batch_id);


-- ============================================================
-- TABLE: usage_events
--
-- Per-event cost tracking for billing and operator monitoring.
-- ============================================================

CREATE TABLE IF NOT EXISTS usage_events (
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

CREATE INDEX IF NOT EXISTS idx_usage_events_user_date
    ON usage_events (user_id, created_at DESC);


-- ============================================================
-- ENABLE ROW LEVEL SECURITY
-- Must appear before the CREATE POLICY statements below.
-- ============================================================

ALTER TABLE subscriptions        ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE products             ENABLE ROW LEVEL SECURITY;
ALTER TABLE repricing_jobs       ENABLE ROW LEVEL SECURITY;
ALTER TABLE price_history        ENABLE ROW LEVEL SECURITY;
ALTER TABLE batch_results        ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_events         ENABLE ROW LEVEL SECURITY;


-- ============================================================
-- RLS POLICIES — CLIENT (auth.uid())
-- ============================================================

-- subscriptions: users may only read their own row
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'subscriptions'
          AND policyname = 'Users read own subscription'
    ) THEN
        CREATE POLICY "Users read own subscription"
        ON subscriptions FOR SELECT
        USING (user_id = auth.uid());
    END IF;
END $$;

-- platform_connections: full CRUD on own rows
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'platform_connections'
          AND policyname = 'Users manage own platform connections'
    ) THEN
        CREATE POLICY "Users manage own platform connections"
        ON platform_connections FOR ALL
        USING (user_id = auth.uid());
    END IF;
END $$;

-- products: full CRUD on own rows
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'products'
          AND policyname = 'Users manage own products'
    ) THEN
        CREATE POLICY "Users manage own products"
        ON products FOR ALL
        USING (user_id = auth.uid());
    END IF;
END $$;

-- repricing_jobs: read-only for clients
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'repricing_jobs'
          AND policyname = 'Users read own repricing jobs'
    ) THEN
        CREATE POLICY "Users read own repricing jobs"
        ON repricing_jobs FOR SELECT
        USING (user_id = auth.uid());
    END IF;
END $$;

-- price_history: read-only for clients
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'price_history'
          AND policyname = 'Users read own price history'
    ) THEN
        CREATE POLICY "Users read own price history"
        ON price_history FOR SELECT
        USING (user_id = auth.uid());
    END IF;
END $$;

-- usage_events: read-only for clients
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'usage_events'
          AND policyname = 'Users read own usage'
    ) THEN
        CREATE POLICY "Users read own usage"
        ON usage_events FOR SELECT
        USING (user_id = auth.uid());
    END IF;
END $$;


-- ============================================================
-- RLS POLICIES — SERVICE ROLE (workers / webhook handler)
-- ============================================================

-- subscriptions: service role has full write access (Stripe webhook only)
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'subscriptions'
          AND policyname = 'Service role manages subscriptions'
    ) THEN
        CREATE POLICY "Service role manages subscriptions"
        ON subscriptions FOR ALL
        USING (auth.role() = 'service_role');
    END IF;
END $$;

-- repricing_jobs: workers write job state transitions
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'repricing_jobs'
          AND policyname = 'Service role manages repricing jobs'
    ) THEN
        CREATE POLICY "Service role manages repricing jobs"
        ON repricing_jobs FOR ALL
        USING (auth.role() = 'service_role');
    END IF;
END $$;

-- price_history: price applicator worker writes records
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'price_history'
          AND policyname = 'Service role manages price history'
    ) THEN
        CREATE POLICY "Service role manages price history"
        ON price_history FOR ALL
        USING (auth.role() = 'service_role');
    END IF;
END $$;

-- batch_results: service role only — users never access this directly
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'batch_results'
          AND policyname = 'Service role only'
    ) THEN
        CREATE POLICY "Service role only"
        ON batch_results FOR ALL
        USING (auth.role() = 'service_role');
    END IF;
END $$;

-- usage_events: workers write cost tracking records
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'usage_events'
          AND policyname = 'Service role manages usage'
    ) THEN
        CREATE POLICY "Service role manages usage"
        ON usage_events FOR ALL
        USING (auth.role() = 'service_role');
    END IF;
END $$;


-- ============================================================
-- AUTOMATED MAINTENANCE (pg_cron)
-- ============================================================

-- Purge batch_results older than 30 days (daily at 3:00 AM UTC)
SELECT cron.schedule(
    'purge-old-batch-results',
    '0 3 * * *',
    $$DELETE FROM batch_results WHERE created_at < NOW() - INTERVAL '30 days'$$
);

-- Reset reprice_cycle_count to 0 at midnight UTC
SELECT cron.schedule(
    'reset-reprice-counts',
    '0 0 * * *',
    $$UPDATE products SET reprice_cycle_count = 0$$
);

-- Archive price_history older than 1 year (weekly, Sunday 4:00 AM UTC)
SELECT cron.schedule(
    'archive-old-price-history',
    '0 4 * * 0',
    $$DELETE FROM price_history WHERE applied_at < NOW() - INTERVAL '1 year'$$
);
