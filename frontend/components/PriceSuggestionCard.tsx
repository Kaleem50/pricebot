// PriceSuggestionCard — renders an AI repricing suggestion.
// Displays every required field from ARCHITECTURE.md §6.2.
// Never shows raw JSON, DB IDs, or technical field names.

'use client'

import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import type { PriceSuggestionDisplay, RepricingStrategy } from '@/lib/types'

interface PriceSuggestionCardProps {
  suggestion: PriceSuggestionDisplay
  onApply?: () => void
  applying?: boolean
  isStarterTier?: boolean
}

const strategyLabels: Record<RepricingStrategy, string> = {
  undercut: 'Slight undercut',
  match:    'Price match',
  premium:  'Premium hold',
  hold:     'Hold current',
}

const strategyColors: Record<RepricingStrategy, string> = {
  undercut: 'bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-900/20 dark:text-amber-300 dark:border-amber-800',
  match:    'bg-blue-50 text-blue-700 border-blue-200 dark:bg-blue-900/20 dark:text-blue-300 dark:border-blue-800',
  premium:  'bg-purple-50 text-purple-700 border-purple-200 dark:bg-purple-900/20 dark:text-purple-300 dark:border-purple-800',
  hold:     'bg-gray-50 text-gray-700 border-gray-200 dark:bg-gray-800 dark:text-gray-300 dark:border-gray-700',
}

function ConfidenceBadge({ confidence }: { confidence: number }) {
  const color =
    confidence >= 80
      ? 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300'
      : confidence >= 60
      ? 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/40 dark:text-yellow-300'
      : 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300'

  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium ${color}`}>
      <span className="font-bold">{confidence}%</span>
      confidence
    </span>
  )
}

function PriceLine({
  label,
  value,
  highlight = false,
  muted = false,
}: {
  label: string
  value: string
  highlight?: boolean
  muted?: boolean
}) {
  return (
    <div className="flex items-center justify-between py-2">
      <span className={`text-sm ${muted ? 'text-gray-400 dark:text-gray-500' : 'text-gray-600 dark:text-gray-300'}`}>
        {label}
      </span>
      <span
        className={`text-sm font-semibold ${
          highlight
            ? 'text-blue-700 text-base dark:text-blue-400'
            : muted
            ? 'text-gray-400 dark:text-gray-500'
            : 'text-gray-900 dark:text-gray-100'
        }`}
      >
        {value}
      </span>
    </div>
  )
}

export function PriceSuggestionCard({
  suggestion,
  onApply,
  applying = false,
  isStarterTier = false,
}: PriceSuggestionCardProps) {
  const { currentPrice, suggestedPrice, competitorBenchmark, marginFloor, strategy, confidence, reasoning, appliedAt } =
    suggestion

  const delta = suggestedPrice - currentPrice
  const deltaSign = delta > 0 ? '+' : ''
  const deltaText = `${deltaSign}$${Math.abs(delta).toFixed(2)}`
  const deltaColor =
    delta < 0 ? 'text-green-600 dark:text-green-400' :
    delta > 0 ? 'text-amber-600 dark:text-amber-400' :
                'text-gray-500 dark:text-gray-400'

  const fmt = (n: number) => `$${n.toFixed(2)}`

  return (
    <div className={`rounded-xl border p-5 ${strategyColors[strategy]}`}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wider opacity-70">
            AI Recommendation
          </p>
          <p className="mt-1 text-2xl font-bold text-gray-900 dark:text-gray-100">
            {fmt(suggestedPrice)}
          </p>
          <p className={`text-sm font-medium ${deltaColor}`}>{deltaText} from current</p>
        </div>
        <div className="flex flex-col items-end gap-2">
          <Badge variant="neutral">{strategyLabels[strategy]}</Badge>
          <ConfidenceBadge confidence={confidence} />
        </div>
      </div>

      <div className="mt-4 divide-y divide-current/10 rounded-lg bg-white/60 px-3 dark:bg-black/20">
        <PriceLine label="Your current price"      value={fmt(currentPrice)} />
        <PriceLine label="Suggested price"         value={fmt(suggestedPrice)} highlight />
        {competitorBenchmark !== null && competitorBenchmark !== undefined && (
          <PriceLine label="Lowest competitor" value={fmt(competitorBenchmark)} />
        )}
        <PriceLine label="Your floor (protected)" value={fmt(marginFloor)} muted />
      </div>

      {reasoning && (
        <p className="mt-4 text-sm leading-relaxed text-gray-700 dark:text-gray-300">
          <span className="font-medium">Why: </span>{reasoning}
        </p>
      )}

      {appliedAt ? (
        <p className="mt-3 text-xs text-gray-500 dark:text-gray-400">
          Applied {new Date(appliedAt).toLocaleString()}
        </p>
      ) : isStarterTier && onApply ? (
        <div className="mt-4">
          <Button onClick={onApply} loading={applying} size="sm" className="w-full">
            Apply this price
          </Button>
          <p className="mt-1.5 text-center text-xs text-gray-500 dark:text-gray-400">
            Growth and Pro plans apply prices automatically
          </p>
        </div>
      ) : null}
    </div>
  )
}
