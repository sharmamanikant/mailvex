export type Suppression = { id: string; email: string; reason: string; source: string; effective_at: string }

export type SuppressionType = 'UNSUBSCRIBED' | 'HARD_BOUNCE' | 'COMPLAINT' | 'MANUAL' | 'ADMIN_BLOCKED'

export type SuppressionEntry = {
  id: string
  email: string
  type: SuppressionType
  source: string
  reason: string | null
  provider: string | null
  campaign_id: string | null
  protected: boolean
  active: boolean
  created_at: string | null
  updated_at: string | null
}

export const SUPPRESSION_TYPES: SuppressionType[] = ['UNSUBSCRIBED', 'HARD_BOUNCE', 'COMPLAINT', 'MANUAL', 'ADMIN_BLOCKED']

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, { credentials: 'include', ...init, headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) } })
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: unknown } | null
    const detail = payload?.detail
    throw new Error(typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : 'Suppression request failed')
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const suppressionApi = {
  list: (search: string, reason: string, token: string) => request<Suppression[]>(`/suppressions?search=${encodeURIComponent(search)}&reason=${encodeURIComponent(reason)}`, { headers: { Authorization: `Bearer ${token}` } }),
  remove: (id: string, token: string) => request<void>(`/suppressions/${id}`, { method: 'DELETE', headers: { Authorization: `Bearer ${token}` } }),
  entries: (type: string, search: string, includeInactive: boolean, token: string) => request<SuppressionEntry[]>(`/suppression-entries?type=${encodeURIComponent(type)}&search=${encodeURIComponent(search)}&include_inactive=${includeInactive}`, { headers: { Authorization: `Bearer ${token}` } }),
  addEntry: (payload: { email: string; type: Exclude<SuppressionType, 'UNSUBSCRIBED' | 'HARD_BOUNCE' | 'COMPLAINT'>; reason?: string }, token: string) => request<SuppressionEntry>('/suppression-entries', { method: 'POST', body: JSON.stringify(payload), headers: { Authorization: `Bearer ${token}` } }),
  removeEntry: (id: string, token: string) => request<void>(`/suppression-entries/${id}`, { method: 'DELETE', headers: { Authorization: `Bearer ${token}` } }),
}

export const SUPPRESSION_TAB_LABELS: { value: string; label: string; countKey: string }[] = [
  { value: '', label: 'All', countKey: '' },
  { value: 'UNSUBSCRIBED', label: 'Unsubscribed', countKey: 'UNSUBSCRIBED' },
  { value: 'HARD_BOUNCE', label: 'Bounced', countKey: 'HARD_BOUNCE' },
  { value: 'COMPLAINT', label: 'Complaints', countKey: 'COMPLAINT' },
  { value: 'MANUAL', label: 'Manual', countKey: 'MANUAL' },
]
