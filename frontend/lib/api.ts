// Typed API client wrapping all backend endpoints.
// Every call attaches the Supabase JWT in the Authorization header.
// Throws ApiError on non-2xx responses so callers can handle errors uniformly.

import { getAccessToken } from '@/lib/supabase'
import type {
  ApplyPriceResponse,
  AuthResponse,
  ConnectPlatformRequest,
  ConnectPlatformResponse,
  Platform,
  PortalResponse,
  ProductDetail,
  ProductListItem,
  RegistrationResponse,
  SubscriptionResponse,
  SyncResponse,
  UpdateSettingsRequest,
  PlatformConnectionResponse,
} from '@/lib/types'

const BASE_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string
  ) {
    super(detail)
    this.name = 'ApiError'
  }
}

async function request<T>(
  path: string,
  options: RequestInit = {},
  authenticated = true
): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options.headers as Record<string, string>),
  }

  if (authenticated) {
    const token = await getAccessToken()
    if (!token) throw new ApiError(401, 'Not authenticated')
    headers['Authorization'] = `Bearer ${token}`
  }

  const res = await fetch(`${BASE_URL}${path}`, {
    ...options,
    headers,
  })

  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const body = await res.json()
      detail = body.detail ?? detail
    } catch {
      // ignore parse error
    }
    throw new ApiError(res.status, detail)
  }

  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

// ---- Auth -------------------------------------------------------------------

export const auth = {
  register: (email: string, password: string): Promise<RegistrationResponse> =>
    request<RegistrationResponse>(
      '/auth/register',
      { method: 'POST', body: JSON.stringify({ email, password }) },
      false
    ),

  login: (email: string, password: string): Promise<AuthResponse> =>
    request<AuthResponse>(
      '/auth/login',
      { method: 'POST', body: JSON.stringify({ email, password }) },
      false
    ),

  refresh: (refresh_token: string): Promise<AuthResponse> =>
    request<AuthResponse>(
      '/auth/refresh',
      { method: 'POST', body: JSON.stringify({ refresh_token }) },
      false
    ),
}

// ---- Products ---------------------------------------------------------------

export const products = {
  list: (params?: {
    platform?: Platform
    state?: string
    is_tracking?: boolean
  }): Promise<ProductListItem[]> => {
    const qs = new URLSearchParams()
    if (params?.platform) qs.set('platform', params.platform)
    if (params?.state) qs.set('state', params.state)
    if (params?.is_tracking !== undefined) qs.set('is_tracking', String(params.is_tracking))
    const query = qs.toString() ? `?${qs}` : ''
    return request<ProductListItem[]>(`/products${query}`)
  },

  get: (id: string): Promise<ProductDetail> =>
    request<ProductDetail>(`/products/${id}`),

  updateSettings: (id: string, body: UpdateSettingsRequest): Promise<void> =>
    request<void>(`/products/${id}/settings`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  applyPrice: (id: string): Promise<ApplyPriceResponse> =>
    request<ApplyPriceResponse>(`/products/${id}/apply`, { method: 'POST' }),
}

// ---- Platforms --------------------------------------------------------------

export const platforms = {
  list: (): Promise<PlatformConnectionResponse[]> =>
    request<PlatformConnectionResponse[]>('/platforms'),

  connect: (platform: Platform, body: ConnectPlatformRequest): Promise<ConnectPlatformResponse> =>
    request<ConnectPlatformResponse>(`/platforms/${platform}/connect`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  disconnect: (platform: Platform): Promise<void> =>
    request<void>(`/platforms/${platform}`, { method: 'DELETE' }),

  sync: (platform: Platform): Promise<SyncResponse> =>
    request<SyncResponse>(`/platforms/${platform}/sync`, { method: 'POST' }),
}

// ---- Billing ----------------------------------------------------------------

export const billing = {
  getSubscription: (): Promise<SubscriptionResponse> =>
    request<SubscriptionResponse>('/billing/subscription'),

  createPortalSession: (): Promise<PortalResponse> =>
    request<PortalResponse>('/billing/portal', { method: 'POST' }),
}
