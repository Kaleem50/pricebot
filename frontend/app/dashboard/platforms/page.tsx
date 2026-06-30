'use client'

import { useEffect, useState } from 'react'
import { platforms as platformsApi } from '@/lib/api'
import type { PlatformConnectionResponse } from '@/lib/types'
import { Card } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { PlatformWizard } from '@/components/PlatformWizard'
import { ApiError } from '@/lib/api'

const PLATFORM_LABELS: Record<string, string> = {
  amazon: 'Amazon', etsy: 'Etsy', shopify: 'Shopify', ebay: 'eBay', woocommerce: 'WooCommerce',
}

function ConnectionCard({
  connection,
  onSync,
  onDisconnect,
  syncing,
  disconnecting,
}: {
  connection: PlatformConnectionResponse
  onSync: () => void
  onDisconnect: () => void
  syncing: boolean
  disconnecting: boolean
}) {
  return (
    <Card>
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-4">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-blue-50 text-blue-700 font-bold text-lg dark:bg-blue-900/30 dark:text-blue-400">
            {PLATFORM_LABELS[connection.platform]?.[0] ?? '?'}
          </div>
          <div>
            <p className="font-semibold text-gray-900 dark:text-gray-100">{PLATFORM_LABELS[connection.platform] ?? connection.platform}</p>
            <p className="text-xs text-gray-500 dark:text-gray-400">
              {connection.product_count} products •{' '}
              {connection.last_validated_at
                ? `Last validated ${new Date(connection.last_validated_at).toLocaleDateString()}`
                : 'Not yet validated'}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className={`flex h-2 w-2 rounded-full ${connection.is_active ? 'bg-green-500' : 'bg-red-400'}`} />
          <span className="text-xs text-gray-500 dark:text-gray-400">{connection.is_active ? 'Active' : 'Inactive'}</span>
          <Button variant="secondary" size="sm" onClick={onSync} loading={syncing}>
            Sync products
          </Button>
          <Button variant="danger" size="sm" onClick={onDisconnect} loading={disconnecting}>
            Disconnect
          </Button>
        </div>
      </div>
    </Card>
  )
}

export default function PlatformsPage() {
  const [connections, setConnections] = useState<PlatformConnectionResponse[]>([])
  const [loading, setLoading] = useState(true)
  const [showWizard, setShowWizard] = useState(false)
  const [syncingPlatform, setSyncingPlatform] = useState<string | null>(null)
  const [disconnectingPlatform, setDisconnectingPlatform] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [disconnectConfirm, setDisconnectConfirm] = useState<string | null>(null)

  function load() {
    setLoading(true)
    platformsApi.list().then(setConnections).catch(() => {}).finally(() => setLoading(false))
  }

  useEffect(load, [])

  async function handleSync(platform: string) {
    setSyncingPlatform(platform)
    setActionError(null)
    try {
      const result = await platformsApi.sync(platform as never)
      alert(`Sync complete — ${result.products_synced} products updated.`)
      load()
    } catch (e: unknown) {
      setActionError(e instanceof ApiError ? e.detail : 'Sync failed')
    } finally {
      setSyncingPlatform(null)
    }
  }

  async function handleDisconnect(platform: string) {
    if (disconnectConfirm !== platform) {
      setDisconnectConfirm(platform)
      return
    }
    setDisconnectingPlatform(platform)
    setDisconnectConfirm(null)
    setActionError(null)
    try {
      await platformsApi.disconnect(platform as never)
      load()
    } catch (e: unknown) {
      setActionError(e instanceof ApiError ? e.detail : 'Disconnect failed')
    } finally {
      setDisconnectingPlatform(null)
    }
  }

  if (showWizard) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Connect a store</h1>
        <Card padding="lg">
          <PlatformWizard
            onComplete={() => {
              setShowWizard(false)
              load()
            }}
            onCancel={() => setShowWizard(false)}
          />
        </Card>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Connected platforms</h1>
        <Button onClick={() => setShowWizard(true)}>Connect a store</Button>
      </div>

      {actionError && (
        <div className="rounded-lg bg-red-50 dark:bg-red-900/30 p-3 text-sm text-red-800 dark:text-red-300">{actionError}</div>
      )}

      {disconnectConfirm && (
        <div className="rounded-xl border border-red-300 bg-red-50 p-4 text-sm dark:border-red-800 dark:bg-red-900/20">
          <p className="font-medium text-red-900 dark:text-red-300">
            Are you sure you want to disconnect {PLATFORM_LABELS[disconnectConfirm] ?? disconnectConfirm}?
          </p>
          <p className="mt-1 text-red-700 dark:text-red-400">
            All products from this platform will stop being monitored. This cannot be undone easily.
          </p>
          <div className="mt-3 flex gap-2">
            <Button variant="danger" size="sm" onClick={() => handleDisconnect(disconnectConfirm)}>
              Yes, disconnect
            </Button>
            <Button variant="secondary" size="sm" onClick={() => setDisconnectConfirm(null)}>
              Cancel
            </Button>
          </div>
        </div>
      )}

      {loading ? (
        <div className="flex h-48 items-center justify-center">
          <div className="h-8 w-8 animate-spin rounded-full border-4 border-blue-600 border-t-transparent" />
        </div>
      ) : connections.length === 0 ? (
        <Card className="py-12 text-center">
          <p className="text-gray-500 dark:text-gray-400 text-sm">No platforms connected yet.</p>
          <p className="mt-1 text-gray-400 dark:text-gray-500 text-xs">Click &quot;Connect a store&quot; to get started.</p>
        </Card>
      ) : (
        <div className="space-y-4">
          {connections.map((c) => (
            <ConnectionCard
              key={c.platform}
              connection={c}
              onSync={() => handleSync(c.platform)}
              onDisconnect={() => handleDisconnect(c.platform)}
              syncing={syncingPlatform === c.platform}
              disconnecting={disconnectingPlatform === c.platform}
            />
          ))}
        </div>
      )}
    </div>
  )
}
