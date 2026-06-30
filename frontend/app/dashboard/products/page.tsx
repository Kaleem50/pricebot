'use client'

import { useEffect, useState } from 'react'
import { products as productsApi } from '@/lib/api'
import type { ProductListItem, Platform } from '@/lib/types'
import { ProductTable } from '@/components/ProductTable'
import { EmptyState } from '@/components/EmptyState'

const PLATFORMS: { value: Platform | ''; label: string }[] = [
  { value: '', label: 'All platforms' },
  { value: 'amazon', label: 'Amazon' },
  { value: 'etsy', label: 'Etsy' },
  { value: 'shopify', label: 'Shopify' },
  { value: 'ebay', label: 'eBay' },
  { value: 'woocommerce', label: 'WooCommerce' },
]

const STATES = [
  { value: '', label: 'All statuses' },
  { value: 'IDLE', label: 'Idle' },
  { value: 'SYNCED', label: 'Up to date' },
  { value: 'BATCH_SUBMITTED', label: 'Analyzing' },
  { value: 'FAILED', label: 'Failed' },
]

const selectCls =
  'rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-700 ' +
  'focus:outline-none focus:ring-2 focus:ring-blue-500 ' +
  'dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300'

export default function ProductsPage() {
  const [productList, setProductList] = useState<ProductListItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [platform, setPlatform] = useState<Platform | ''>('')
  const [state, setState] = useState('')

  useEffect(() => {
    setLoading(true)
    setError(null)
    productsApi
      .list({ platform: platform || undefined, state: state || undefined })
      .then(setProductList)
      .catch(() => setError('Failed to load products — please refresh or contact support.'))
      .finally(() => setLoading(false))
  }, [platform, state])

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Products</h1>
        <p className="text-sm text-gray-500 dark:text-gray-400">
          {!loading && `${productList.length} products`}
        </p>
      </div>

      <div className="flex flex-wrap gap-3">
        <select value={platform} onChange={(e) => setPlatform(e.target.value as Platform | '')} className={selectCls}>
          {PLATFORMS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
        </select>
        <select value={state} onChange={(e) => setState(e.target.value)} className={selectCls}>
          {STATES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
        </select>
      </div>

      {error && (
        <div className="rounded-lg bg-red-50 dark:bg-red-900/30 p-3 text-sm text-red-800 dark:text-red-300">{error}</div>
      )}

      {loading ? (
        <div className="flex h-48 items-center justify-center">
          <div className="h-8 w-8 animate-spin rounded-full border-4 border-blue-600 border-t-transparent" />
        </div>
      ) : error ? null : productList.length === 0 ? (
        <EmptyState
          title="No products yet"
          description="Connect your Amazon account to import your product catalog."
          ctaLabel="Connect a store"
          ctaHref="/dashboard/platforms"
          icon={
            <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round"
                d="M20.25 7.5l-.625 10.632a2.25 2.25 0 01-2.247 2.118H6.622a2.25 2.25 0 01-2.247-2.118L3.75 7.5M10 11.25h4M3.375 7.5h17.25c.621 0 1.125-.504 1.125-1.125v-1.5c0-.621-.504-1.125-1.125-1.125H3.375c-.621 0-1.125.504-1.125 1.125v1.5c0 .621.504 1.125 1.125 1.125z" />
            </svg>
          }
        />
      ) : (
        <ProductTable products={productList} />
      )}
    </div>
  )
}
