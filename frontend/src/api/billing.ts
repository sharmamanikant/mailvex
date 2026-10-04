import type { BillingPlan, UsageEventRow, UsageOverview } from '../types/billing'

async function request<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, { credentials: 'include', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, ...(init?.headers ?? {}) }, ...init })
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: string } | null
    throw new Error(payload?.detail ?? 'Usage request failed')
  }
  return response.json() as Promise<T>
}

export const usageApi = {
  overview: (token: string) => request<UsageOverview>('/usage/overview', token),
  plans: (token: string) => request<BillingPlan[]>('/usage/plans', token),
  events: (token: string, limit = 50) => request<UsageEventRow[]>(`/usage/events?limit=${limit}`, token),
}