import type { JobState, SubscriptionTier } from '@/lib/types'

interface BadgeProps {
  children: React.ReactNode
  variant?: 'default' | 'success' | 'warning' | 'error' | 'info' | 'neutral'
  size?: 'sm' | 'md'
}

const variantClasses = {
  default: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300',
  success: 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300',
  warning: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/40 dark:text-yellow-300',
  error:   'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300',
  info:    'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300',
  neutral: 'bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300',
}

const sizeClasses = {
  sm: 'px-2 py-0.5 text-xs',
  md: 'px-2.5 py-1 text-sm',
}

export function Badge({ children, variant = 'default', size = 'sm' }: BadgeProps) {
  return (
    <span
      className={[
        'inline-flex items-center rounded-full font-medium',
        variantClasses[variant],
        sizeClasses[size],
      ].join(' ')}
    >
      {children}
    </span>
  )
}

export function StateBadge({ state }: { state: JobState }) {
  const map: Record<JobState, { variant: BadgeProps['variant']; label: string }> = {
    IDLE:            { variant: 'neutral',  label: 'Idle' },
    BATCH_SUBMITTED: { variant: 'info',     label: 'Analyzing…' },
    PROCESSING:      { variant: 'info',     label: 'Applying…' },
    SYNCED:          { variant: 'success',  label: 'Up to date' },
    FAILED:          { variant: 'error',    label: 'Failed' },
  }
  const { variant, label } = map[state] ?? { variant: 'neutral', label: state }
  return <Badge variant={variant}>{label}</Badge>
}

export function TierBadge({ tier }: { tier: SubscriptionTier }) {
  const map: Record<SubscriptionTier, { variant: BadgeProps['variant']; label: string }> = {
    starter: { variant: 'neutral', label: 'Starter' },
    growth:  { variant: 'info',    label: 'Growth' },
    pro:     { variant: 'success', label: 'Pro' },
  }
  const { variant, label } = map[tier] ?? { variant: 'neutral', label: tier }
  return <Badge variant={variant}>{label}</Badge>
}
