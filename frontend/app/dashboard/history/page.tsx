'use client'

// Price history page — fetches from repricing/history endpoint.
// Shows full audit log of all price changes with AI reasoning.

import { useEffect, useState } from 'react'
import { EmptyState } from '@/components/EmptyState'
import { getAccessToken } from '@/lib/supabase'
import { ApiError } from '@/lib/api'

interface PriceHistoryEntry {
  id: string
  product_title: string
  platform: string
  old_price: number
  new_price: number
  strategy: string | null
  reasoning: string | null
  confidence: number | null
  was_auto_applied: boolean
  applied_at: string
}

const STRATEGY_LABELS: Record<string, string> = {
  undercut: 'Slight undercut',
  match:    'Price match',
  premium:  'Premium hold',
  hold:     'Hold current',
}

export default function HistoryPage() {
  const [entries, setEntries] = useState<PriceHistoryEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const BASE_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
    getAccessToken().then((token) => {
      if (!token) { setLoading(false); return }
      return fetch(`${BASE_URL}/repricing/history`, {
        headers: { Authorization: `Bearer ${token}` },
      })
        .then((r) => {
          if (!r.ok) throw new ApiError(r.status, 'Failed to load history')
          return r.json()
        })
        .then((data) => setEntries(data ?? []))
        .catch((e: unknown) => setError(e instanceof Error ? e.message : 'Error'))
    })
      .finally(() => setLoading(false))
  }, [])

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-blue-600 border-t-transparent" />
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Price history</h1>
      <p className="text-sm text-gray-500 dark:text-gray-400">Every price change PriceBot has made, with AI reasoning.</p>

      {error && (
        <div className="rounded-lg bg-red-50 dark:bg-red-900/30 p-3 text-sm text-red-800 dark:text-red-300">{error}</div>
      )}

      {entries.length === 0 && !error ? (
        <EmptyState
          title="No price changes yet"
          description="Price history will appear here after PriceBot completes its first repricing cycle."
          icon={
            <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
          }
        />
      ) : (
        <div className="space-y-3">
          {entries.map((entry) => {
            const delta = entry.new_price - entry.old_price
            const deltaText = `${delta >= 0 ? '+' : ''}$${Math.abs(delta).toFixed(2)}`
            const deltaColor =
              delta < 0
                ? 'text-green-700 dark:text-green-400'
                : delta > 0
                ? 'text-amber-700 dark:text-amber-400'
                : 'text-gray-500 dark:text-gray-400'
            return (
              <div
                key={entry.id}
                className="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900"
              >
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0 flex-1">
                    <p className="truncate font-medium text-gray-900 dark:text-gray-100">{entry.product_title}</p>
                    <p className="text-xs text-gray-400 dark:text-gray-500 mt-0.5">
                      {new Date(entry.applied_at).toLocaleString()} ·{' '}
                      {STRATEGY_LABELS[entry.strategy ?? ''] ?? entry.strategy ?? 'Unknown strategy'}
                      {entry.was_auto_applied ? ' · Auto-applied' : ' · Manually applied'}
                    </p>
                    {entry.reasoning && (
                      <p className="mt-2 text-sm text-gray-600 dark:text-gray-300">{entry.reasoning}</p>
                    )}
                  </div>
                  <div className="text-right shrink-0">
                    <p className="text-sm text-gray-500 dark:text-gray-400 line-through">${entry.old_price.toFixed(2)}</p>
                    <p className="text-lg font-bold text-gray-900 dark:text-gray-100">${entry.new_price.toFixed(2)}</p>
                    <p className={`text-sm font-medium ${deltaColor}`}>{deltaText}</p>
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
