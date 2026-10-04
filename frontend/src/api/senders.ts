import type { ReconnectResult, Sender, SenderInput } from '../types/senders'

export type SenderHealth = {
  sender_id: string
  status: string
  state: string
  health_score: number
  factors: Record<string, unknown>
  score_contributions: Record<string, number>
  failure_reasons: string[]
  summary: string
  disclaimer: string
  history?: {
    current: { status: string; state: string; health_score: number | null; checked_at: string | null }
    avg_7d: number | null
    avg_30d: number | null
    failure_reasons: string[]
    samples: number
  }
}

export type DomainRecord = {
  id: string
  tenant_id: string
  domain: string
  health_status: string
  created_at: string | null
  updated_at: string | null
}

export type DomainHealth = {
  domain_id: string
  domain: string
  status: string
  checks: Record<string, { status: string; message: string; remediation: string }>
  summary: string
  remediation: string
  disclaimer: string
}

export type DomainHistory = {
  domain_id: string
  samples: number
  history: Array<{ status: string; summary: string; checks: Record<string, unknown>; checked_at: string }>
}

async function request<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, { credentials: 'include', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, ...(init?.headers ?? {}) }, ...init })
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: string } | null
    throw new Error(payload?.detail ?? 'Sender request failed')
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const sendersApi = {
  connectGoogle: (token: string) => request<{ authorization_url: string }>('/email-senders/google/connect', token),
  connectMicrosoft: (token: string) => request<{ authorization_url: string }>('/email-senders/microsoft/connect', token),
  list: (token: string) => request<Sender[]>('/email-senders', token),
  get: (id: string, token: string) => request<Sender>(`/email-senders/${id}`, token),
  create: (input: SenderInput, token: string) => request<{ sender: Sender; provider_authenticated: boolean }>('/email-senders', token, { method: 'POST', body: JSON.stringify(input) }),
  update: (id: string, input: Partial<SenderInput>, token: string) => request<Sender>(`/email-senders/${id}`, token, { method: 'PATCH', body: JSON.stringify(input) }),
  disconnect: (id: string, token: string) => request<Sender>(`/email-senders/${id}/disconnect`, token, { method: 'POST' }),
  healthCheck: (id: string, token: string) => request<{ sender_id: string; status: string; health_score: number | null; healthy: boolean }>(`/email-senders/${id}/health-check`, token, { method: 'POST' }),
  getHealth: (id: string, token: string) => request<SenderHealth>(`/email-senders/${id}/health`, token),
  testConnection: (id: string, token: string) => request<{ sender_id: string; successful: boolean; message: string }>(`/email-senders/${id}/test-connection`, token, { method: 'POST' }),
  enable: (id: string, token: string) => request<{ sender: Sender; provider_authenticated: boolean }>(`/email-senders/${id}/enable`, token, { method: 'POST' }),
  testEmail: (id: string, recipient: string, token: string) => request<{ sender_id: string; successful: boolean; message: string }>(`/email-senders/${id}/test-email`, token, { method: 'POST', body: JSON.stringify({ recipient }) }),
  reconnect: (id: string, token: string) => request<ReconnectResult>(`/email-senders/${id}/reconnect`, token, { method: 'POST' }),
}
