import type { User } from '../types/auth'

export type AdminUser = User & { status: string; created_at: string; role?: string }
export type AdminRole = { id: string; name: string; description: string | null; is_system: boolean }
export type AdminTeam = { id: string; name: string; created_at: string }
export type AdminAuditLog = { id: string; action: string; entity_type: string | null; entity_id: string | null; actor_user_id: string | null; request_id: string | null; timestamp: string; metadata: Record<string, unknown> }

async function request<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, { credentials: 'include', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, ...init })
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: string } | null
    throw new Error(payload?.detail ?? 'Administration request failed')
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const adminApi = {
  users: (token: string) => request<{ items: AdminUser[]; total: number }>('/admin/users?page_size=100', token),
  createUser: (payload: { email: string; display_name: string; password: string; role: string }, token: string) => request<AdminUser>('/admin/users', token, { method: 'POST', body: JSON.stringify(payload) }),
  updateUser: (id: string, payload: { display_name?: string; status?: string; role?: string }, token: string) => request<AdminUser>(`/admin/users/${id}`, token, { method: 'PATCH', body: JSON.stringify(payload) }),
  updatePassword: (id: string, password: string, token: string) => request<void>(`/admin/users/${id}/password`, token, { method: 'POST', body: JSON.stringify({ password }) }),
  deleteUser: (id: string, token: string) => request<void>(`/admin/users/${id}`, token, { method: 'DELETE' }),
  roles: (token: string) => request<AdminRole[]>('/admin/roles', token),
  teams: (token: string) => request<{ items: AdminTeam[]; total: number }>('/admin/teams?page_size=100', token),
  createTeam: (name: string, token: string) => request<AdminTeam>('/admin/teams', token, { method: 'POST', body: JSON.stringify({ name }) }),
  auditLogs: (token: string) => request<{ items: AdminAuditLog[]; total: number }>('/admin/audit-logs?page_size=50', token),
}