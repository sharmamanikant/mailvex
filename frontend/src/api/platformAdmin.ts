export type PlatformOverview = { workspaces: number; users: number; active_users: number; disabled_users: number }
export type PlatformWorkspace = { id: string; name: string; slug: string; status: string; created_at: string }
export type PlatformUser = { id: string; tenant_id: string; email: string; display_name: string; status: string; role: string | null; created_at: string }

async function request<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, { credentials: 'include', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, ...init })
  if (!response.ok) { const payload = await response.json().catch(() => null) as { detail?: string } | null; throw new Error(payload?.detail ?? 'Platform administration request failed') }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const platformAdminApi = {
  overview: (token: string) => request<PlatformOverview>('/platform-admin/overview', token),
  workspaces: (token: string) => request<{ items: PlatformWorkspace[]; total: number }>('/platform-admin/workspaces?page_size=100', token),
  users: (token: string) => request<{ items: PlatformUser[]; total: number }>('/platform-admin/users?page_size=200', token),
  updateUser: (id: string, payload: { status?: string; role?: string; display_name?: string }, token: string) => request<PlatformUser>(`/platform-admin/users/${id}`, token, { method: 'PATCH', body: JSON.stringify(payload) }),
  updatePassword: (id: string, password: string, token: string) => request<void>(`/platform-admin/users/${id}/password`, token, { method: 'POST', body: JSON.stringify({ password }) }),
}