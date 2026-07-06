-- 003_beta_requests.sql
-- Creates beta_requests and notifications_sent tables.
-- Fixes usage_events CHECK constraint to include 'api_call' and 'email_sent'.
-- Adds performance indexes missing from earlier migrations.
--
-- Run via: psql $DATABASE_URL -f db/migrations/003_beta_requests.sql
-- Or apply in the Supabase SQL editor.

-- ──────────────────────────────────────────────────────────────────────────────
-- 1. beta_requests
-- ──────────────────────────────────────────────────────────────────────────────
-- Stores waitlist signups from /beta/request.
-- RLS: no user owns these rows — only the service-role key can access them.
-- The frontend form is public; user_id is intentionally not stored (pre-auth).

CREATE TABLE IF NOT EXISTS beta_requests (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email            TEXT NOT NULL UNIQUE,          -- one row per email
    platform         TEXT NOT NULL CHECK (platform IN ('amazon','etsy','shopify','ebay','woocommerce')),
    product_count    INTEGER NOT NULL CHECK (product_count >= 1),
    reprice_frequency TEXT NOT NULL CHECK (reprice_frequency IN ('daily','weekly','manual')),
    status           TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Row-Level Security — service-role bypass only; no anon/authenticated access.
ALTER TABLE beta_requests ENABLE ROW LEVEL SECURITY;

-- Explicitly deny all access from anon and authenticated roles.
-- The API uses the service-role key via db/client.py and bypasses RLS.
DO $$
BEGIN
    CREATE POLICY "beta_requests_deny_all" ON beta_requests
        AS RESTRICTIVE
        FOR ALL
        TO anon, authenticated
        USING (false)
        WITH CHECK (false);
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- ──────────────────────────────────────────────────────────────────────────────
-- 2. notifications_sent
-- ──────────────────────────────────────────────────────────────────────────────
-- Tracks sent price-change emails for debounce (max 1 per product per hour).
-- Owned by user_id — RLS allows users to read only their own rows.

CREATE TABLE IF NOT EXISTS notifications_sent (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    product_id   UUID NOT NULL,  -- FK to products.id enforced at app layer
    email_type   TEXT NOT NULL CHECK (email_type IN ('suggestion_ready','auto_applied')),
    sent_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Row-Level Security
ALTER TABLE notifications_sent ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    CREATE POLICY "notifications_sent_select_own" ON notifications_sent
        FOR SELECT
        TO authenticated
        USING (user_id = auth.uid());
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- Workers run as service-role and bypass RLS — no insert policy needed for authenticated.

-- ──────────────────────────────────────────────────────────────────────────────
-- 3. Fix usage_events CHECK constraint
-- ──────────────────────────────────────────────────────────────────────────────
-- The existing constraint only covers a subset of values already in the table.
-- Adding a narrower constraint would violate existing rows.
-- Strategy:
--   a) DROP the old constraint unconditionally (IF EXISTS makes it safe).
--   b) ADD the new constraint covering all known values — both the original
--      set ('batch_submitted', 'batch_completed', 'price_applied',
--      'credential_validated', 'sync_completed') and the new ones
--      ('api_call', 'email_sent') needed by the Etsy connector and
--      notifications module.
--   c) Wrap ADD in a DO block so re-running is a no-op.

ALTER TABLE usage_events
    DROP CONSTRAINT IF EXISTS usage_events_event_type_check;

DO $$
BEGIN
    ALTER TABLE usage_events
        ADD CONSTRAINT usage_events_event_type_check
        CHECK (event_type IN (
            'batch_submitted',
            'batch_completed',
            'price_applied',
            'credential_validated',
            'sync_completed',
            'api_call',
            'email_sent'
        ));
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- ──────────────────────────────────────────────────────────────────────────────
-- 4. Performance indexes
-- ──────────────────────────────────────────────────────────────────────────────

-- beta_requests: operator list view sorts by created_at DESC
CREATE INDEX IF NOT EXISTS idx_beta_requests_created_at
    ON beta_requests (created_at DESC);

-- notifications_sent: debounce query hits user_id + product_id + sent_at
CREATE INDEX IF NOT EXISTS idx_notifications_sent_lookup
    ON notifications_sent (user_id, product_id, sent_at DESC);

-- repricing_jobs: worker polls for IDLE jobs per user
CREATE INDEX IF NOT EXISTS idx_repricing_jobs_state_user
    ON repricing_jobs (state, user_id);

-- repricing_jobs: poller polls by batch_id
CREATE INDEX IF NOT EXISTS idx_repricing_jobs_batch_id
    ON repricing_jobs (batch_id)
    WHERE batch_id IS NOT NULL;

-- price_history: dashboard chart queries by product_id + applied_at
CREATE INDEX IF NOT EXISTS idx_price_history_product_recorded
    ON price_history (product_id, applied_at DESC);

-- products: scheduler queries active products per user
CREATE INDEX IF NOT EXISTS idx_products_user_id
    ON products (user_id);

-- usage_events: rate-limit queries hit user_id + event_type + platform + created_at
CREATE INDEX IF NOT EXISTS idx_usage_events_rate_limit
    ON usage_events (user_id, event_type, platform, created_at DESC);

-- ──────────────────────────────────────────────────────────────────────────────
-- 5. updated_at trigger for beta_requests
-- ──────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS set_beta_requests_updated_at ON beta_requests;
CREATE TRIGGER set_beta_requests_updated_at
    BEFORE UPDATE ON beta_requests
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();
