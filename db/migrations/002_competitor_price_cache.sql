-- ============================================================
-- 002_competitor_price_cache.sql — Competitor Price Cache Columns
--
-- Adds two columns to the products table for caching competitor
-- price data returned by platform APIs.
--
-- Design:
--   competitor_prices_cached_at — timestamp of last successful API fetch
--   competitor_prices_cache     — serialised JSON array of competitor offers
--
-- Cache staleness threshold: 15 minutes.
-- Workers check competitor_prices_cached_at before calling the platform
-- API; if the cache is fresh they skip the API call entirely.
-- ============================================================

ALTER TABLE products
  ADD COLUMN IF NOT EXISTS competitor_prices_cached_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS competitor_prices_cache     JSONB DEFAULT '[]'::JSONB;

-- Index for workers that query stale products first
CREATE INDEX IF NOT EXISTS idx_products_price_cache_staleness
  ON products (user_id, competitor_prices_cached_at NULLS FIRST)
  WHERE is_tracking = TRUE AND state = 'IDLE';

COMMENT ON COLUMN products.competitor_prices_cached_at IS
  'UTC timestamp of last competitor price API fetch. NULL = never fetched.';

COMMENT ON COLUMN products.competitor_prices_cache IS
  'Cached competitor price offers as a JSON array of CompetitorProduct objects.
   Considered stale after 15 minutes. Never used for pricing decisions if stale.';
