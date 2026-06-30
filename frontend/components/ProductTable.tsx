'use client'

import Link from 'next/link'
import { StateBadge } from '@/components/ui/Badge'
import type { ProductListItem } from '@/lib/types'

interface ProductTableProps {
  products: ProductListItem[]
}

const PLATFORM_LABELS: Record<string, string> = {
  amazon:      'Amazon',
  etsy:        'Etsy',
  shopify:     'Shopify',
  ebay:        'eBay',
  woocommerce: 'WooCommerce',
}

function formatDate(iso: string | null) {
  if (!iso) return '—'
  return new Intl.DateTimeFormat('en-US', {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  }).format(new Date(iso))
}

export function ProductTable({ products }: ProductTableProps) {
  if (products.length === 0) return null

  return (
    <div className="overflow-hidden rounded-xl border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-900">
      <table className="min-w-full divide-y divide-gray-100 dark:divide-gray-800">
        <thead>
          <tr className="bg-gray-50 dark:bg-gray-800/50">
            <th className="py-3 pl-5 pr-3 text-left text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
              Product
            </th>
            <th className="px-3 py-3 text-left text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
              Platform
            </th>
            <th className="px-3 py-3 text-right text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
              Current Price
            </th>
            <th className="px-3 py-3 text-left text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
              Status
            </th>
            <th className="px-3 py-3 text-left text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
              Last Updated
            </th>
            <th className="py-3 pl-3 pr-5 text-right text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
              Action
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-50 dark:divide-gray-800">
          {products.map((product) => (
            <tr
              key={product.id}
              className="hover:bg-gray-50/50 dark:hover:bg-gray-800/40 transition-colors"
            >
              <td className="max-w-xs py-3 pl-5 pr-3">
                <p className="truncate text-sm font-medium text-gray-900 dark:text-gray-100">
                  {product.title}
                </p>
                <p className="truncate text-xs text-gray-400 dark:text-gray-500">
                  {product.platform_product_id}
                </p>
              </td>
              <td className="px-3 py-3">
                <span className="text-sm text-gray-700 dark:text-gray-300">
                  {PLATFORM_LABELS[product.platform] ?? product.platform}
                </span>
              </td>
              <td className="px-3 py-3 text-right">
                <span className="text-sm font-semibold text-gray-900 dark:text-gray-100">
                  ${product.current_price.toFixed(2)}
                </span>
              </td>
              <td className="px-3 py-3">
                <StateBadge state={product.state} />
              </td>
              <td className="px-3 py-3">
                <span className="text-xs text-gray-500 dark:text-gray-400">
                  {formatDate(product.last_repriced_at)}
                </span>
              </td>
              <td className="py-3 pl-3 pr-5 text-right">
                <Link
                  href={`/dashboard/products/${product.id}`}
                  className="text-xs font-medium text-blue-600 hover:text-blue-800 dark:text-blue-400 dark:hover:text-blue-300"
                >
                  View
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
