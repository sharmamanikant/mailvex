async function request<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
      ...(init?.headers ?? {}),
    },
    ...init,
  })

  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { detail?: string } | null
    throw new Error(payload?.detail ?? 'Analytics request failed')
  }

  return response.json() as Promise<T>
}

export type AnalyticsMetrics = {
  campaigns: number
  recipients: number
  queued: number
  sent: number
  provider_accepted: number
  bounced: number
  failed: number
  replies: number
  positive_replies: number
  unsubscribes: number
  complaints: number
}

export type AnalyticsDashboard = {
  tenant_id: string
  metrics: AnalyticsMetrics
  campaign_performance: Array<Record<string, unknown>>
  sender_performance: Array<Record<string, unknown>>
  domain_health: Array<Record<string, unknown>>
}

export const analyticsApi = {
  dashboard: (token: string) => request<AnalyticsDashboard>('/analytics/dashboard', token),
  campaigns: (token: string) => request<Array<Record<string, unknown>>>('/analytics/campaigns', token),
  senders: (token: string) => request<Array<Record<string, unknown>>>('/analytics/senders', token),
  domains: (token: string) => request<Array<Record<string, unknown>>>('/analytics/domains', token),
}
