import { redirect } from 'next/navigation'

// Root route — always redirect to dashboard (middleware handles auth gate)
export default function HomePage() {
  redirect('/dashboard')
}
