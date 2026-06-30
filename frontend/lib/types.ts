// TypeScript types mirroring backend Pydantic models exactly.
// Field names match the JSON snake_case keys the API returns.

export type Platform = 'amazon' | 'etsy' | 'shopify' | 'ebay' | 'woocommerce'

export type JobState = 'IDLE' | 'BATCH_SUBMITTED' | 'PROCESSING' | 'SYNCED' | 'FAILED'

export type SubscriptionTier = 'starter' | 'growth' | 'pro'

export type SubscriptionStatus = 'active' | 'trialing' | 'past_due' | 'canceled'

export type RepricingStrategy = 'undercut' | 'match' | 'premium' | 'hold'

// ---- Auth -------------------------------------------------------------------

export interface AuthResponse {
  access_token: string
  refresh_token: string
  user_id: string
  email: string
}

export interface RegistrationResponse {
  user_id: string
  email: string
  message: string
}

// ---- Products ---------------------------------------------------------------

export interface PriceSuggestion {
  id: string
  suggested_price: number
  strategy: string | null
  confidence: number | null
  reasoning: string | null
  competitor_low: number | null
  was_auto_applied: boolean
  applied_at: string
}

export interface ProductListItem {
  id: string
  title: string
  platform: Platform
  platform_product_id: string
  current_price: number
  state: JobState
  is_tracking: boolean
  last_repriced_at: string | null
}

export interface ProductDetail {
  id: string
  title: string
  platform: Platform
  platform_product_id: string
  platform_sku: string | null
  current_price: number
  cost: number | null
  min_margin_floor: number
  state: JobState
  is_tracking: boolean
  last_repriced_at: string | null
  last_synced_at: string | null
  reprice_cycle_count: number
  fail_reason: string | null
  last_suggestion: PriceSuggestion | null
}

export interface UpdateSettingsRequest {
  min_margin_floor?: number
  is_tracking?: boolean
}

export interface ApplyPriceResponse {
  product_id: string
  previous_price: number
  applied_price: number
  strategy: string | null
  message: string
}

// ---- Platforms --------------------------------------------------------------

export interface PlatformConnectionResponse {
  platform: Platform
  is_active: boolean
  created_at: string
  last_validated_at: string | null
  product_count: number
}

export interface ConnectPlatformRequest {
  credentials: Record<string, string>
}

export interface ConnectPlatformResponse {
  platform: Platform
  is_active: boolean
  message: string
}

export interface SyncResponse {
  platform: Platform
  products_synced: number
  message: string
}

// ---- Billing ----------------------------------------------------------------

export interface SubscriptionResponse {
  tier: SubscriptionTier
  status: SubscriptionStatus
  current_period_end: string | null
  product_count: number
  product_limit: number
  reprice_cycles_today: number
  daily_cycle_limit: number
}

export interface PortalResponse {
  url: string
}

// ---- PriceSuggestionCard display contract (ARCHITECTURE.md §6.2) ------------

export interface PriceSuggestionDisplay {
  currentPrice: number
  suggestedPrice: number
  competitorBenchmark: number | null
  marginFloor: number
  strategy: RepricingStrategy
  confidence: number
  reasoning: string
  appliedAt?: string
}
