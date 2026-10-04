import type { TokenResponse, User } from '../types/auth'

const API_ROOT = '/api/v1'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_ROOT}${path}`, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    ...init,
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: string } | null
    throw new Error(payload?.detail ?? 'Request failed')
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const authApi = {
  me: (accessToken: string) => request<User>('/auth/me', { headers: { Authorization: `Bearer ${accessToken}` } }),
  login: (email: string, password: string) => request<TokenResponse>('/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) }),
  signup: (displayName: string, email: string, password: string) => request<{ access_token: string; expires_at: string; user: User }>('/auth/signup', { method: 'POST', body: JSON.stringify({ display_name: displayName, email, password }) }),
  refresh: () => request<TokenResponse>('/auth/refresh', { method: 'POST' }),
  logout: () => request<void>('/auth/logout', { method: 'POST' }),
}

export type PolicyStatus = {
  policy_type: string
  title: string
  summary: string
  policy_version: string
  accepted: boolean
  accepted_version: string | null
  accepted_at: string | null
}

export type PoliciesStatus = { required: boolean; documents: PolicyStatus[] }
export type PolicyDocument = PolicyStatus & { body: string; published_at: string | null }
export type ComplianceProfile = {
  compliance_profile: string
  jurisdiction: string
  require_unsubscribe: boolean
  require_sender_identity: boolean
  require_policy_acceptance: boolean
  require_consent_metadata: boolean
  require_list_unsubscribe_header: boolean
  retention_policy: Record<string, unknown>
  safety_thresholds: Record<string, unknown>
}

export const policyApi = {
  status: (accessToken: string) => request<PoliciesStatus>('/policies/status', { headers: { Authorization: `Bearer ${accessToken}` } }),
  document: (policyType: string, accessToken: string) => request<PolicyDocument>(`/policies/documents/${policyType}`, { headers: { Authorization: `Bearer ${accessToken}` } }),
  accept: (policyType: string, accessToken: string) => request<PolicyStatus>(`/policies/documents/${policyType}/accept`, { method: 'POST', headers: { Authorization: `Bearer ${accessToken}` } }),
  profile: (accessToken: string) => request<ComplianceProfile>('/compliance/profile', { headers: { Authorization: `Bearer ${accessToken}` } }),
  updateProfile: (payload: Partial<ComplianceProfile>, accessToken: string) => request<ComplianceProfile>('/compliance/profile', { method: 'PATCH', headers: { Authorization: `Bearer ${accessToken}` }, body: JSON.stringify(payload) }),
}