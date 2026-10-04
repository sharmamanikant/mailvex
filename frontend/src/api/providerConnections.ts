import type { GoogleConnectResponse, ProviderConnection } from '../types/providerConnections'

async function request<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, ...(init?.headers ?? {}) },
    ...init,
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: string } | null
    throw new Error(payload?.detail ?? 'Provider connection request failed')
  }
  return response.json() as Promise<T>
}

export const providerConnectionsApi = {
  list: (token: string) => request<ProviderConnection[]>('/provider-connections', token),
  startGoogle: (token: string) => request<GoogleConnectResponse>('/provider-connections/google/connect', token, { method: 'POST' }),
  startMicrosoft: (token: string) => request<GoogleConnectResponse>('/provider-connections/microsoft/connect', token, { method: 'POST' }),
  disconnect: (id: string, token: string) => request<ProviderConnection>(`/provider-connections/${id}`, token, { method: 'DELETE' }),
  refresh: (id: string, token: string) => request<ProviderConnection>(`/provider-connections/${id}/refresh`, token, { method: 'POST' }),
}