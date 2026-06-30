'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { products as productsApi, billing } from '@/lib/api'
import type { ProductListItem, SubscriptionResponse } from '@/lib/types'
import { Card, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { EmptyState } from '@/components/EmptyState'
import { StateBadge } from '@/components/ui/Badge'

interface StatCardProps {
  label: string
  value: string | number
  sublabel?: string
  color?: string
}

function StatCard({ label, value, sublabel, color = 'text-gray-900 dark:text-gray-100' }: StatCardProps) {
  return (
    <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm dark:border-gray-700 dark:bg-gray-900">
      <p className="text-sm text-gray-500 dark:text-gray-400">{label}</p>
      <p className={`mt-1 text-3xl font-bold ${color}`}>{value}</p>
      {sublabel && <p className="mt-1 text-xs text-gray-400 dark:text-gray-500">{sublabel}</p>}
    </div>
  )
}

export default function DashboardPage() {
  const [productList, setProductList] = useState<ProductListItem[]>([])
  const [sub, setSub] = useState<SubscriptionResponse | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    Promise.all([productsApi.list(), billing.getSubscription()])
      .then(([p, s]) => { setProductList(p); setSub(s) })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-blue-600 border-t-transparent" />
      </div>
    )
  }

  const totalProducts = productList.length
  const syncedToday = productList.filter(
    (p) =>
      p.state === 'SYNCED' &&
      p.last_repriced_at &&
      new Date(p.last_repriced_at).toDateString() === new Date().toDateString()
  ).length
  const failedJobs = productList.filter((p) => p.state === 'FAILED').length

  if (totalProducts === 0) {
    return (
      <div className="flex h-full flex-col items-center justify-center py-20">
        <EmptyState
          title="Connect your first store"
          description="Link your Amazon account and PriceBot will start monitoring competitor prices automatically."
          ctaLabel="Connect a store"
          ctaHref="/dashboard/platforms"
          icon={
            <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round"
                d="M13.19 8.688a4.5 4.5 0 011.242 7.244l-4.5 4.5a4.5 4.5 0 01-6.364-6.364l1.757-1.757m13.35-.622l1.757-1.757a4.5 4.5 0 00-6.364-6.364l-4.5 4.5a4.5 4.5 0 001.242 7.244" />
            </svg>
          }
        />
      </div>
    )
  }

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Overview</h1>
        <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
          {new Date().toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric' })}
        </p>
      </div>

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <StatCard label="Products tracked" value={totalProducts} />
        <StatCard
          label="Repriced today"
          value={syncedToday}
          sublabel={sub ? `${sub.reprice_cycles_today}/${sub.daily_cycle_limit} cycles used` : undefined}
          color="text-blue-700 dark:text-blue-400"
        />
        <StatCard
          label="Cycles remaining"
          value={sub ? Math.max(0, sub.daily_cycle_limit - sub.reprice_cycles_today) : '—'}
          sublabel="resets at midnight"
        />
        {failedJobs > 0 && (
          <StatCard label="Failed jobs" value={failedJobs} color="text-red-600 dark:text-red-400" />
        )}
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Recent products</CardTitle>
          <Link href="/dashboard/products">
            <Button variant="ghost" size="sm">View all</Button>
          </Link>
        </CardHeader>
        <div className="divide-y divide-gray-50 dark:divide-gray-800">
          {productList.slice(0, 5).map((p) => (
            <Link
              key={p.id}
              href={`/dashboard/products/${p.id}`}
              className="flex items-center justify-between py-3 hover:bg-gray-50/50 dark:hover:bg-gray-800/40 -mx-2 px-2 rounded-lg transition-colors"
            >
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium text-gray-900 dark:text-gray-100">{p.title}</p>
                <p className="text-xs text-gray-400 dark:text-gray-500">
                  {p.last_repriced_at
                    ? `Updated ${new Date(p.last_repriced_at).toLocaleTimeString()}`
                    : 'Not yet repriced'}
                </p>
              </div>
              <div className="ml-4 flex items-center gap-3 shrink-0">
                <span className="text-sm font-semibold text-gray-900 dark:text-gray-100">
                  ${p.current_price.toFixed(2)}
                </span>
                <StateBadge state={p.state} />
              </div>
            </Link>
          ))}
        </div>
      </Card>
    </div>
  )
}
