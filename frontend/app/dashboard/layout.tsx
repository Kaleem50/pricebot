'use client'

import { Component, useEffect, useState } from 'react'
import type { ReactNode, ErrorInfo } from 'react'
import { useRouter } from 'next/navigation'
import { getSupabaseClient } from '@/lib/supabase'
import { DashboardNav } from '@/components/DashboardNav'

// ---------------------------------------------------------------------------
// Error boundary — catches unhandled render errors in any dashboard page.
// Displays a recoverable error card instead of crashing the whole app.
// ---------------------------------------------------------------------------

interface ErrorBoundaryState {
  hasError: boolean
  message: string
}

class DashboardErrorBoundary extends Component<
  { children: ReactNode },
  ErrorBoundaryState
> {
  constructor(props: { children: ReactNode }) {
    super(props)
    this.state = { hasError: false, message: '' }
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, message: error.message ?? 'An unexpected error occurred.' }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Log to console in dev; a real error monitoring service (e.g. Sentry) would
    // be wired here in production.
    console.error('[DashboardErrorBoundary]', error, info.componentStack)
  }

  handleReset = () => {
    this.setState({ hasError: false, message: '' })
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex flex-1 items-center justify-center p-8">
          <div className="max-w-md w-full text-center">
            <div className="text-4xl mb-4">⚠️</div>
            <h2 className="text-xl font-semibold text-gray-900 dark:text-white mb-2">
              Something went wrong
            </h2>
            <p className="text-sm text-gray-500 dark:text-gray-400 mb-6">
              {this.state.message}
            </p>
            <button
              onClick={this.handleReset}
              className="inline-flex items-center px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-700
                         text-white text-sm font-medium transition-colors"
            >
              Try again
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}

// ---------------------------------------------------------------------------
// Layout
// ---------------------------------------------------------------------------

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter()
  const [checking, setChecking] = useState(true)

  useEffect(() => {
    const supabase = getSupabaseClient()
    supabase.auth.getSession().then(({ data }) => {
      if (!data.session) {
        router.replace('/login')
      } else {
        setChecking(false)
      }
    })
  }, [router])

  if (checking) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-gray-50 dark:bg-gray-950">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-blue-600 border-t-transparent" />
      </div>
    )
  }

  return (
    <div className="flex min-h-screen bg-gray-50 dark:bg-gray-950">
      <DashboardNav />
      <main className="flex-1 overflow-y-auto p-8">
        <DashboardErrorBoundary>
          {children}
        </DashboardErrorBoundary>
      </main>
    </div>
  )
}
