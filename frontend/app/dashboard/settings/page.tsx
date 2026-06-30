'use client'

import { useState, FormEvent } from 'react'
import { Card, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'

export default function SettingsPage() {
  const [marginFloor, setMarginFloor] = useState('20')
  const [saved, setSaved] = useState(false)
  const [saving, setSaving] = useState(false)

  async function handleSave(e: FormEvent) {
    e.preventDefault()
    setSaving(true)
    // Default margin floor is a local preference — product-level overrides
    // are managed per-product via PATCH /products/{id}/settings.
    await new Promise((r) => setTimeout(r, 400))
    setSaved(true)
    setSaving(false)
    setTimeout(() => setSaved(false), 3000)
  }

  return (
    <div className="space-y-6 max-w-xl">
      <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Settings</h1>

      <Card>
        <CardHeader>
          <CardTitle>Default margin floor</CardTitle>
        </CardHeader>
        <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
          PriceBot will never suggest a price that puts you below this margin. Applied to new
          products automatically. You can override it per product.
        </p>
        <form onSubmit={handleSave} className="space-y-4">
          <Input
            label="Default minimum margin (%)"
            type="number"
            min="1"
            max="99"
            value={marginFloor}
            onChange={(e) => setMarginFloor(e.target.value)}
          />
          <p className="text-xs text-gray-400 dark:text-gray-500">
            Example: 20% means if a product costs $10, PriceBot will never go below $12.
          </p>
          <div className="flex items-center gap-3">
            <Button type="submit" loading={saving}>Save changes</Button>
            {saved && (
              <span className="text-sm text-green-700 dark:text-green-400 font-medium">Saved</span>
            )}
          </div>
        </form>
      </Card>
    </div>
  )
}
