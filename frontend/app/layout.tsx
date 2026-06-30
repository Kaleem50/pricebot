import type { Metadata } from 'next'
import './globals.css'

export const metadata: Metadata = {
  title: 'PriceBot — AI-Powered Repricing',
  description: 'Monitor competitor prices and auto-reprice your listings with AI.',
}

// Runs before React hydration — sets 'dark' class from system preference
// so there's no flash of wrong theme. No localStorage (per constraints).
const themeScript = `
(function(){
  try {
    if (window.matchMedia('(prefers-color-scheme: dark)').matches) {
      document.documentElement.classList.add('dark');
    }
  } catch(e) {}
})();
`

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body className="bg-gray-50 text-gray-900 antialiased dark:bg-gray-950 dark:text-gray-50">
        {children}
      </body>
    </html>
  )
}
