'use client'

import { useEffect, useState } from 'react'
import { billing } from '@/lib/api'
import type { SubscriptionResponse } from '@/lib/types'
import { Card, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { TierBadge } from '@/components/ui/Badge'
import { ApiError } from '@/lib/api'

const TIER_DESCRIPTIONS: Record<string, { products: string; cycles: string; price: string; autoApply: boolean }> = {
  starter: {
    products: 'Up to 50 products',
    cycles:   '3× per day',
    price:    '$9/month',
    autoApply: false,
  },
  growth: {
    products: 'Up to 500 products',
    cycles:   '6× per day',
    price:    '$29/month',
    autoApply: true,
  },
  pro: {
    products: 'Up to 10,000 products',
    cycles:   '12× per day',
    price:    '$59/month',
    autoApply: true,
  },
}

function UsageBar({ value, max, label }: { value: number; max: number; label: string }) {
  const pct = Math.min(100, max > 0 ? Math.round((value / max) * 100) : 0)
  const color = pct >= 90 ? 'bg-red-500' : pct >= 70 ? 'bg-amber-400' : 'bg-blue-500'
  return (
    <div>
      <div className="flex justify-between text-xs text-gray-500 dark:text-gray-400 mb-1">
        <span>{label}</span>
        <span>{value} / {max}</span>
      </div>
      <div className="h-2 rounded-full bg-gray-100 dark:bg-gray-700 overflow-hidden">
        <div className={`h-2 rounded-full ${color} transition-all`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

export default function BillingPage() {
  const [sub, setSub] = useState<SubscriptionResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [portalLoading, setPortalLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    billing.getSubscription().then(setSub).catch(() => {}).finally(() => setLoading(false))
  }, [])

  async function handleManageBilling() {
    setPortalLoading(true)
    setError(null)
    try {
      const { url } = await billing.createPortalSession()
      window.location.href = url
    } catch (e: unknown) {
      setError(e instanceof ApiError ? e.detail : 'Could not open billing portal')
      setPortalLoading(false)
    }
  }

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-blue-600 border-t-transparent" />
      </div>
    )
  }

  const tierInfo = sub ? TIER_DESCRIPTIONS[sub.tier] ?? null : null

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Billing</h1>

      {error && (
        <div className="rounded-lg bg-red-50 dark:bg-red-900/30 p-3 text-sm text-red-800 dark:text-red-300">{error}</div>
      )}

      {/* Current plan */}
      <Card>
        <CardHeader>
          <CardTitle>Current plan</CardTitle>
          {sub && <TierBadge tier={sub.tier} />}
        </CardHeader>
        {sub && tierInfo ? (
          <div className="space-y-4">
            <div className="flex flex-wrap gap-6 text-sm">
              <div>
                <p className="text-gray-500 dark:text-gray-400">Plan</p>
                <p className="font-semibold text-gray-900 dark:text-gray-100 capitalize">{sub.tier}</p>
              </div>
              <div>
                <p className="text-gray-500 dark:text-gray-400">Price</p>
                <p className="font-semibold text-gray-900 dark:text-gray-100">{tierInfo.price}</p>
              </div>
              <div>
                <p className="text-gray-500 dark:text-gray-400">Status</p>
                <p className={`font-semibold capitalize ${
                  sub.status === 'active' || sub.status === 'trialing'
                    ? 'text-green-700 dark:text-green-400'
                    : 'text-red-700 dark:text-red-400'
                }`}>
                  {sub.status}
                </p>
              </div>
              {sub.current_period_end && (
                <div>
                  <p className="text-gray-500 dark:text-gray-400">Renews</p>
                  <p className="font-semibold text-gray-900 dark:text-gray-100">
                    {new Date(sub.current_period_end).toLocaleDateString('en-US', {
                      month: 'long', day: 'numeric', year: 'numeric',
                    })}
                  </p>
                </div>
              )}
              <div>
                <p className="text-gray-500 dark:text-gray-400">Auto-apply prices</p>
                <p className={`font-semibold ${
                  tierInfo.autoApply
                    ? 'text-green-700 dark:text-green-400'
                    : 'text-gray-500 dark:text-gray-400'
                }`}>
                  {tierInfo.autoApply ? 'Yes' : 'Manual only'}
                </p>
              </div>
            </div>

            {/* Usage */}
            <div className="space-y-3 rounded-xl bg-gray-50 dark:bg-gray-800/50 p-4">
              <p className="text-sm font-medium text-gray-700 dark:text-gray-300">Usage today</p>
              <UsageBar
                value={sub.product_count}
                max={sub.product_limit}
                label="Products tracked"
              />
              <UsageBar
                value={sub.reprice_cycles_today}
                max={sub.daily_cycle_limit}
                label="Reprice cycles"
              />
            </div>

            <Button onClick={handleManageBilling} loading={portalLoading} variant="secondary">
              Manage billing
            </Button>
          </div>
        ) : (
          <div className="text-sm text-gray-500 dark:text-gray-400">No active subscription found.</div>
        )}
      </Card>

      {/* Upgrade CTA (only shown if not on Pro) */}
      {sub && sub.tier !== 'pro' && (
        <Card className="border-blue-200 bg-blue-50 dark:border-blue-800 dark:bg-blue-900/20">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <p className="font-semibold text-blue-900 dark:text-blue-300">
                {sub.tier === 'starter' ? 'Upgrade to Growth' : 'Upgrade to Pro'}
              </p>
              <p className="mt-1 text-sm text-blue-700 dark:text-blue-400">
                {sub.tier === 'starter'
                  ? 'Get automatic price application, 500 products, and 6 cycles per day.'
                  : 'Scale to 10,000 products with 12 reprice cycles per day.'}
              </p>
            </div>
            <Button onClick={handleManageBilling} loading={portalLoading} size="sm">
              Upgrade now
            </Button>
          </div>
        </Card>
      )}
    </div>
  )
}
