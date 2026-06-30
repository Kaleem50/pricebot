'use client'

import { useEffect, useState } from 'react'
import { useParams, useRouter } from 'next/navigation'
import Link from 'next/link'
import { products as productsApi } from '@/lib/api'
import type { ProductDetail } from '@/lib/types'
import { Card, CardHeader, CardTitle } from '@/components/ui/Card'
import { StateBadge } from '@/components/ui/Badge'
import { PriceSuggestionCard } from '@/components/PriceSuggestionCard'
import type { PriceSuggestionDisplay, RepricingStrategy } from '@/lib/types'
import { ApiError } from '@/lib/api'

const PLATFORM_LABELS: Record<string, string> = {
  amazon: 'Amazon', etsy: 'Etsy', shopify: 'Shopify', ebay: 'eBay', woocommerce: 'WooCommerce',
}

export default function ProductDetailPage() {
  const { id } = useParams<{ id: string }>()
  const router = useRouter()
  const [product, setProduct] = useState<ProductDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [applying, setApplying] = useState(false)
  const [applyError, setApplyError] = useState<string | null>(null)
  const [applySuccess, setApplySuccess] = useState(false)

  useEffect(() => {
    productsApi.get(id).then(setProduct).catch(() => router.push('/dashboard/products')).finally(() => setLoading(false))
  }, [id, router])

  async function handleApply() {
    if (!product) return
    setApplying(true)
    setApplyError(null)
    try {
      await productsApi.applyPrice(product.id)
      setApplySuccess(true)
      const updated = await productsApi.get(product.id)
      setProduct(updated)
    } catch (e: unknown) {
      setApplyError(e instanceof ApiError ? e.detail : 'Failed to apply price')
    } finally {
      setApplying(false)
    }
  }

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-blue-600 border-t-transparent" />
      </div>
    )
  }
  if (!product) return null

  const suggestion = product.last_suggestion
  const suggestionDisplay: PriceSuggestionDisplay | null = suggestion
    ? {
        currentPrice: product.current_price,
        suggestedPrice: suggestion.suggested_price,
        competitorBenchmark: suggestion.competitor_low,
        marginFloor: product.min_margin_floor,
        strategy: (suggestion.strategy as RepricingStrategy) ?? 'hold',
        confidence: suggestion.confidence ?? 0,
        reasoning: suggestion.reasoning ?? '',
        appliedAt: suggestion.was_auto_applied ? suggestion.applied_at : undefined,
      }
    : null

  return (
    <div className="space-y-6">
      {/* Breadcrumb */}
      <div className="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
        <Link href="/dashboard/products" className="hover:text-gray-900 dark:hover:text-gray-200">Products</Link>
        <span>/</span>
        <span className="truncate text-gray-900 dark:text-gray-100">{product.title}</span>
      </div>

      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100 leading-snug">{product.title}</h1>
          <div className="mt-2 flex items-center gap-3">
            <span className="text-sm text-gray-500 dark:text-gray-400">{PLATFORM_LABELS[product.platform] ?? product.platform}</span>
            <span className="text-gray-300 dark:text-gray-600">•</span>
            <StateBadge state={product.state} />
          </div>
        </div>
        <div className="text-right shrink-0">
          <p className="text-xs text-gray-400 dark:text-gray-500">Current price</p>
          <p className="text-3xl font-bold text-gray-900 dark:text-gray-100">${product.current_price.toFixed(2)}</p>
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-5">
        {/* AI suggestion (2/5 width on lg) */}
        <div className="lg:col-span-2">
          {suggestionDisplay ? (
            <>
              <PriceSuggestionCard
                suggestion={suggestionDisplay}
                onApply={handleApply}
                applying={applying}
                isStarterTier={!suggestionDisplay.appliedAt}
              />
              {applySuccess && (
                <p className="mt-2 text-sm text-green-700 dark:text-green-400 font-medium">Price applied successfully.</p>
              )}
              {applyError && (
                <p className="mt-2 text-sm text-red-700 dark:text-red-400">{applyError}</p>
              )}
            </>
          ) : (
            <div className="rounded-xl border border-dashed border-gray-300 dark:border-gray-700 p-6 text-center text-sm text-gray-400 dark:text-gray-500">
              <p className="font-medium">No suggestion yet</p>
              <p className="mt-1">PriceBot will analyze this product in the next repricing cycle (every 15 min).</p>
            </div>
          )}
        </div>

        {/* Product details (3/5 width on lg) */}
        <div className="space-y-4 lg:col-span-3">
          <Card>
            <CardHeader><CardTitle>Product details</CardTitle></CardHeader>
            <dl className="divide-y divide-gray-50 dark:divide-gray-800 text-sm">
              {[
                { label: 'SKU', value: product.platform_sku ?? '—' },
                { label: 'Platform ID', value: product.platform_product_id },
                { label: 'Cost', value: product.cost != null ? `$${product.cost.toFixed(2)}` : '—' },
                { label: 'Margin floor', value: `$${product.min_margin_floor.toFixed(2)}` },
                { label: 'Reprice cycles', value: product.reprice_cycle_count },
                {
                  label: 'Last repriced',
                  value: product.last_repriced_at
                    ? new Date(product.last_repriced_at).toLocaleString()
                    : 'Never',
                },
                {
                  label: 'Last synced',
                  value: product.last_synced_at
                    ? new Date(product.last_synced_at).toLocaleString()
                    : '—',
                },
              ].map(({ label, value }) => (
                <div key={label} className="flex justify-between py-2">
                  <dt className="text-gray-500 dark:text-gray-400">{label}</dt>
                  <dd className="font-medium text-gray-900 dark:text-gray-100">{value}</dd>
                </div>
              ))}
            </dl>
            {product.fail_reason && (
              <div className="mt-4 rounded-lg bg-red-50 dark:bg-red-900/30 p-3 text-sm text-red-800 dark:text-red-300">
                <span className="font-medium">Last failure: </span>{product.fail_reason}
              </div>
            )}
          </Card>
        </div>
      </div>
    </div>
  )
}
