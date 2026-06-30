import Link from 'next/link'
import { Button } from '@/components/ui/Button'

interface EmptyStateProps {
  title: string
  description: string
  ctaLabel?: string
  ctaHref?: string
  onCtaClick?: () => void
  icon?: React.ReactNode
}

export function EmptyState({ title, description, ctaLabel, ctaHref, onCtaClick, icon }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      {icon && (
        <div className="mb-4 flex h-14 w-14 items-center justify-center rounded-full bg-blue-50 text-blue-500 dark:bg-blue-900/30 dark:text-blue-400">
          {icon}
        </div>
      )}
      <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">{title}</h3>
      <p className="mt-2 max-w-sm text-sm text-gray-500 dark:text-gray-400">{description}</p>
      {ctaLabel && (
        <div className="mt-6">
          {ctaHref ? (
            <Link href={ctaHref}>
              <Button>{ctaLabel}</Button>
            </Link>
          ) : (
            <Button onClick={onCtaClick}>{ctaLabel}</Button>
          )}
        </div>
      )}
    </div>
  )
}
