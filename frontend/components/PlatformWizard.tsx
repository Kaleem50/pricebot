'use client'

// Amazon connection wizard — 4-step stepper (ARCHITECTURE.md §6.3).
// Step 1: Select platform
// Step 2: Credential instructions
// Step 3: Paste credentials + test connection
// Step 4: Set margin floor defaults

import { useState } from 'react'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { platforms as platformsApi } from '@/lib/api'

interface PlatformWizardProps {
  onComplete: () => void
  onCancel: () => void
}

type Step = 1 | 2 | 3 | 4

const STEP_LABELS = ['Select platform', 'Get credentials', 'Test connection', 'Set margin floor']

function StepIndicator({ current }: { current: Step }) {
  return (
    <ol className="flex items-center gap-0">
      {STEP_LABELS.map((label, i) => {
        const step = (i + 1) as Step
        const done = step < current
        const active = step === current
        return (
          <li key={step} className="flex items-center">
            <span
              className={[
                'flex h-8 w-8 items-center justify-center rounded-full text-sm font-semibold shrink-0',
                done
                  ? 'bg-blue-600 text-white'
                  : active
                  ? 'border-2 border-blue-600 text-blue-600 dark:border-blue-400 dark:text-blue-400'
                  : 'border-2 border-gray-200 text-gray-400 dark:border-gray-700 dark:text-gray-500',
              ].join(' ')}
            >
              {done ? (
                <svg className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
                  <path fillRule="evenodd"
                    d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z"
                    clipRule="evenodd" />
                </svg>
              ) : step}
            </span>
            <span
              className={`ml-2 hidden text-sm sm:block ${
                active
                  ? 'font-medium text-gray-900 dark:text-gray-100'
                  : 'text-gray-400 dark:text-gray-500'
              }`}
            >
              {label}
            </span>
            {step < 4 && (
              <div
                className={`mx-4 h-px w-8 flex-1 ${
                  done ? 'bg-blue-600' : 'bg-gray-200 dark:bg-gray-700'
                }`}
              />
            )}
          </li>
        )
      })}
    </ol>
  )
}

export function PlatformWizard({ onComplete, onCancel }: PlatformWizardProps) {
  const [step, setStep] = useState<Step>(1)
  const [creds, setCreds] = useState({ seller_id: '', mws_auth_token: '', marketplace_id: '' })
  const [marginFloor, setMarginFloor] = useState('20')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<'ok' | 'fail' | null>(null)

  async function handleTestConnection() {
    setLoading(true)
    setError(null)
    setTestResult(null)
    try {
      await platformsApi.connect('amazon', { credentials: creds })
      setTestResult('ok')
    } catch (e: unknown) {
      setTestResult('fail')
      setError(e instanceof Error ? e.message : 'Connection failed')
    } finally {
      setLoading(false)
    }
  }

  async function handleFinish() {
    setLoading(true)
    setError(null)
    try {
      await platformsApi.sync('amazon')
      onComplete()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Sync failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="mx-auto max-w-2xl">
      <div className="mb-8">
        <StepIndicator current={step} />
      </div>

      {/* Step 1 — Select platform */}
      {step === 1 && (
        <div className="space-y-4">
          <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100">Connect a store</h2>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            Choose which platform you want PriceBot to monitor. More platforms coming soon.
          </p>
          <button
            onClick={() => setStep(2)}
            className="flex w-full items-center gap-4 rounded-xl border-2 border-blue-500 bg-blue-50 p-4 text-left transition hover:bg-blue-100 dark:bg-blue-900/20 dark:hover:bg-blue-900/30 dark:border-blue-600"
          >
            <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-white text-xl font-bold shadow-sm dark:bg-gray-800 dark:text-gray-100">A</span>
            <div>
              <p className="font-semibold text-gray-900 dark:text-gray-100">Amazon</p>
              <p className="text-sm text-gray-500 dark:text-gray-400">Seller Central — SP-API</p>
            </div>
            <svg className="ml-auto h-5 w-5 text-blue-500 dark:text-blue-400" viewBox="0 0 20 20" fill="currentColor">
              <path fillRule="evenodd"
                d="M7.293 14.707a1 1 0 010-1.414L10.586 10 7.293 6.707a1 1 0 011.414-1.414l4 4a1 1 0 010 1.414l-4 4a1 1 0 01-1.414 0z"
                clipRule="evenodd" />
            </svg>
          </button>
          <div className="flex items-center gap-4 rounded-xl border border-gray-200 bg-gray-50 p-4 opacity-60 dark:border-gray-700 dark:bg-gray-800">
            <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-white text-xl shadow-sm dark:bg-gray-700">E</span>
            <div>
              <p className="font-semibold text-gray-900 dark:text-gray-200">Etsy</p>
              <p className="text-sm text-gray-500 dark:text-gray-400">Coming in Week 5</p>
            </div>
          </div>
        </div>
      )}

      {/* Step 2 — Credential instructions */}
      {step === 2 && (
        <div className="space-y-4">
          <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100">Get your Amazon credentials</h2>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            We need read-only API access to your Seller Central account. Your credentials are
            encrypted with AES-256-GCM before we store them.
          </p>
          <ol className="space-y-3 rounded-xl bg-amber-50 p-4 text-sm text-amber-900 dark:bg-amber-900/20 dark:text-amber-300">
            <li className="flex gap-2">
              <span className="shrink-0 font-bold">1.</span>
              Log in to <strong>sellercentral.amazon.com</strong>
            </li>
            <li className="flex gap-2">
              <span className="shrink-0 font-bold">2.</span>
              Go to <strong>Settings → User Permissions → Amazon Marketplace Web Service (MWS)</strong>
            </li>
            <li className="flex gap-2">
              <span className="shrink-0 font-bold">3.</span>
              Authorise PriceBot and copy your <strong>Seller ID</strong>, <strong>MWS Auth Token</strong>, and <strong>Marketplace ID</strong>
            </li>
          </ol>
          <div className="flex justify-between pt-2">
            <Button variant="ghost" onClick={() => setStep(1)}>Back</Button>
            <Button onClick={() => setStep(3)}>I have my credentials</Button>
          </div>
        </div>
      )}

      {/* Step 3 — Paste credentials */}
      {step === 3 && (
        <div className="space-y-4">
          <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100">Paste your credentials</h2>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            These are encrypted immediately and never shown in logs.
          </p>
          <div className="space-y-3">
            <Input label="Seller ID" placeholder="A1B2C3D4E5F6G7"
              value={creds.seller_id} onChange={(e) => setCreds((p) => ({ ...p, seller_id: e.target.value }))} />
            <Input label="MWS Auth Token" placeholder="amzn.mws.xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
              value={creds.mws_auth_token} onChange={(e) => setCreds((p) => ({ ...p, mws_auth_token: e.target.value }))} />
            <Input label="Marketplace ID" placeholder="ATVPDKIKX0DER"
              value={creds.marketplace_id} onChange={(e) => setCreds((p) => ({ ...p, marketplace_id: e.target.value }))} />
          </div>
          {testResult === 'ok' && (
            <div className="rounded-lg bg-green-50 p-3 text-sm font-medium text-green-800 dark:bg-green-900/30 dark:text-green-300">
              Connection successful — your account is linked.
            </div>
          )}
          {error && (
            <div className="rounded-lg bg-red-50 p-3 text-sm text-red-800 dark:bg-red-900/30 dark:text-red-300">{error}</div>
          )}
          <div className="flex justify-between pt-2">
            <Button variant="ghost" onClick={() => setStep(2)}>Back</Button>
            {testResult === 'ok' ? (
              <Button onClick={() => setStep(4)}>Continue</Button>
            ) : (
              <Button onClick={handleTestConnection} loading={loading}
                disabled={!creds.seller_id || !creds.mws_auth_token || !creds.marketplace_id}>
                Test connection
              </Button>
            )}
          </div>
        </div>
      )}

      {/* Step 4 — Margin floor default */}
      {step === 4 && (
        <div className="space-y-4">
          <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100">Protect your margins</h2>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            PriceBot will never suggest a price below your margin floor. You can adjust this per product later.
          </p>
          <div className="rounded-xl bg-blue-50 p-4 dark:bg-blue-900/20">
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Minimum margin (% of cost)
            </label>
            <div className="flex items-center gap-3">
              <input type="range" min="5" max="80" step="5" value={marginFloor}
                onChange={(e) => setMarginFloor(e.target.value)}
                className="flex-1 accent-blue-600" />
              <span className="w-12 text-center text-lg font-bold text-blue-700 dark:text-blue-400">{marginFloor}%</span>
            </div>
            <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
              Most sellers use 20–40%. Start conservative — you can lower it later.
            </p>
          </div>
          {error && (
            <div className="rounded-lg bg-red-50 p-3 text-sm text-red-800 dark:bg-red-900/30 dark:text-red-300">{error}</div>
          )}
          <div className="flex justify-between pt-2">
            <Button variant="ghost" onClick={() => setStep(3)}>Back</Button>
            <Button onClick={handleFinish} loading={loading}>Import my products</Button>
          </div>
        </div>
      )}

      <div className="mt-6 border-t border-gray-100 dark:border-gray-800 pt-4">
        <button onClick={onCancel} className="text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-300">
          Cancel
        </button>
      </div>
    </div>
  )
}
