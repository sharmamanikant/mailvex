import type { AlertRuleResult, MetricSnapshot, OpsAlert, OpsOverview, OpsSample } from '../types/ops'

async function request<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, { credentials: 'include', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, ...(init?.headers ?? {}) }, ...init })
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: string } | null
    throw new Error(payload?.detail ?? 'Operations request failed')
  }
  return response.json() as Promise<T>
}

export const opsApi = {
  overview: (token: string) => request<OpsOverview>('/ops/overview', token),
  readiness: (token: string) => request<{ ready: boolean } & Record<string, string>>('/ops/readiness', token),
  metrics: (token: string) => request<{ metrics: Record<string, MetricSnapshot> }>('/ops/metrics', token),
  metricsHistory: (token: string, metric?: string, limit = 100) => {
    const query = new URLSearchParams()
    if (metric) query.set('metric', metric)
    query.set('limit', String(limit))
    return request<{ metric: string | null; samples: OpsSample[] }>(`/ops/metrics/history?${query.toString()}`, token)
  },
  alerts: (token: string, limit = 100) => request<{ alerts: OpsAlert[] }>(`/ops/alerts?limit=${limit}`, token),
  alertsOpen: (token: string) => request<{ alerts: OpsAlert[] }>('/ops/alerts/open', token),
  evaluate: (token: string) => request<{ results: AlertRuleResult[]; created?: number; updated?: number; resolved?: number }>('/ops/alerts/evaluate', token, { method: 'POST' }),
  ack: (token: string, alertId: string) => request<{ alert: OpsAlert }>(`/ops/alerts/${alertId}/ack`, token, { method: 'POST' }),
  sample: (token: string) => request<{ values: Record<string, number | null>; sampled_at: string }>('/ops/sample', token, { method: 'POST' }),
}