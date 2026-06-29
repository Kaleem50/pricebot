-- ============================================================
-- rls_policies.sql — PriceBot Row-Level Security Policies
--
-- Canonical source of truth for all RLS policies.
-- Policies are also applied in db/migrations/001_initial_schema.sql
-- for initial setup; this file can be used to re-apply them.
--
-- Apply with:
--   psql "$SUPABASE_DB_URL" -f db/rls_policies.sql
-- ============================================================


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
--
-- Client: read own subscription row only.
-- Service role: full access (Stripe webhook handler writes all fields).
-- ============================================================

CREATE POLICY "Users read own subscription"
ON subscriptions FOR SELECT
USING (user_id = auth.uid());

CREATE POLICY "Service role manages subscriptions"
ON subscriptions FOR ALL
USING (auth.role() = 'service_role');


-- ============================================================
-- platform_connections
--
-- Client: full CRUD on own rows (connect / update / disconnect).
-- encrypted_creds column is kept server-side; API response schemas
-- must never include this field (enforced in platforms router).
-- ============================================================

CREATE POLICY "Users manage own platform connections"
ON platform_connections FOR ALL
USING (user_id = auth.uid());


-- ============================================================
-- products
--
-- Client: full CRUD on own rows (view, update margin settings, pause tracking).
-- ============================================================

CREATE POLICY "Users manage own products"
ON products FOR ALL
USING (user_id = auth.uid());


-- ============================================================
-- repricing_jobs
--
-- Client: read-only (dashboard job status display).
-- Service role: full write access (scheduler, poller, applicator write states).
-- ============================================================

CREATE POLICY "Users read own repricing jobs"
ON repricing_jobs FOR SELECT
USING (user_id = auth.uid());

CREATE POLICY "Service role manages repricing jobs"
ON repricing_jobs FOR ALL
USING (auth.role() = 'service_role');


-- ============================================================
-- price_history
--
-- Client: read-only (dashboard history view and analytics).
-- Service role: full write access (price applicator worker writes records).
-- ============================================================

CREATE POLICY "Users read own price history"
ON price_history FOR SELECT
USING (user_id = auth.uid());

CREATE POLICY "Service role manages price history"
ON price_history FOR ALL
USING (auth.role() = 'service_role');


-- ============================================================
-- batch_results
--
-- Internal debugging only — users never access this table directly.
-- Service role exclusively (poller writes raw Claude responses).
-- ============================================================

CREATE POLICY "Service role only"
ON batch_results FOR ALL
USING (auth.role() = 'service_role');


-- ============================================================
-- usage_events
--
-- Client: read-only (billing page usage stats).
-- Service role: full write access (workers write cost events after each batch).
-- ============================================================

CREATE POLICY "Users read own usage"
ON usage_events FOR SELECT
USING (user_id = auth.uid());

CREATE POLICY "Service role manages usage"
ON usage_events FOR ALL
USING (auth.role() = 'service_role');
