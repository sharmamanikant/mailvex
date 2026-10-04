import type { DomainHealth, DomainHistory, DomainRecord } from './senders'

async function request<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, { credentials: 'include', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, ...(init?.headers ?? {}) }, ...init })
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: string } | null
    throw new Error(payload?.detail ?? 'Domain request failed')
  }
  return response.json() as Promise<T>
}

export const domainsApi = {
  list: (token: string) => request<{ domains: DomainRecord[]; count: number }>('/domains', token),
  create: (domain: string, token: string) => request<DomainRecord>('/domains', token, { method: 'POST', body: JSON.stringify({ domain }) }),
  getHealth: (id: string, token: string) => request<DomainHealth>(`/domains/${id}/health`, token),
  checkHealth: (id: string, token: string) => request<DomainHealth>(`/domains/${id}/health-check`, token, { method: 'POST' }),
  history: (id: string, token: string) => request<DomainHistory>(`/domains/${id}/history`, token),
}
